from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.events import EventBus, EventType
from app.memory.models import MemoryCategory, MemoryCreate, MemoryRecord


TABLES = {category.value for category in MemoryCategory}
WORDS = re.compile(r"[\wÀ-ÿ-]{2,}", re.UNICODE)


class MemoryRepository:
    def __init__(self, database_path: Path, event_bus: EventBus | None = None) -> None:
        self.database_path = database_path
        self.event_bus = event_bus

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            for table in TABLES:
                role_column = ", role TEXT" if table == MemoryCategory.SHORT_TERM else ""
                await db.execute(
                    f"""CREATE TABLE IF NOT EXISTS {table} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        importance INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
                        metadata TEXT NOT NULL DEFAULT '{{}}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                        {role_column}
                    )"""
                )
            await db.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED, category UNINDEXED, content,
                    tokenize='unicode61 remove_diacritics 2'
                )"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS character_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            await db.execute(
                "INSERT OR IGNORE INTO character_state(singleton, state, updated_at) VALUES(1, 'neutral', ?)",
                (self._now(),),
            )
            await db.commit()

    async def health(self) -> bool:
        try:
            async with aiosqlite.connect(self.database_path) as db:
                row = await (await db.execute("PRAGMA quick_check")).fetchone()
                return bool(row and row[0] == "ok")
        except Exception:
            return False

    async def add(self, memory: MemoryCreate) -> MemoryRecord:
        table = memory.category.value
        now = self._now()
        metadata = json.dumps(memory.metadata, ensure_ascii=False)
        columns = "content, importance, metadata, created_at, updated_at"
        values: list[Any] = [memory.content.strip(), memory.importance, metadata, now, now]
        if memory.category == MemoryCategory.SHORT_TERM:
            columns += ", role"
            values.append(memory.role or "user")
        placeholders = ", ".join("?" for _ in values)
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values
            )
            memory_id = cursor.lastrowid
            await db.execute(
                "INSERT INTO memory_fts(memory_id, category, content) VALUES(?, ?, ?)",
                (memory_id, table, memory.content.strip()),
            )
            await db.commit()
        record = await self.get(memory.category, int(memory_id))
        if self.event_bus:
            await self.event_bus.publish(
                EventType.MEMORY_CREATED,
                category=table,
                memory_id=memory_id,
                importance=memory.importance,
            )
        assert record is not None
        return record

    async def get(self, category: MemoryCategory, memory_id: int) -> MemoryRecord | None:
        role = ", role" if category == MemoryCategory.SHORT_TERM else ""
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    f"SELECT id, content, importance, metadata, created_at, updated_at{role} "
                    f"FROM {category.value} WHERE id = ?",
                    (memory_id,),
                )
            ).fetchone()
        return self._record(category, row) if row else None

    async def list(
        self, category: MemoryCategory, limit: int = 50, offset: int = 0
    ) -> list[MemoryRecord]:
        role = ", role" if category == MemoryCategory.SHORT_TERM else ""
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"SELECT id, content, importance, metadata, created_at, updated_at{role} "
                    f"FROM {category.value} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (min(limit, 200), max(offset, 0)),
                )
            ).fetchall()
        return [self._record(category, row) for row in rows]

    async def recent_conversation(self, limit: int = 12) -> list[MemoryRecord]:
        records = await self.list(MemoryCategory.SHORT_TERM, limit=limit)
        return list(reversed(records))

    async def search(self, query: str, limit: int = 8) -> list[MemoryRecord]:
        terms = WORDS.findall(query)[:10]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            hits = await (
                await db.execute(
                    "SELECT memory_id, category FROM memory_fts WHERE memory_fts MATCH ? "
                    "ORDER BY bm25(memory_fts) LIMIT ?",
                    (fts_query, min(limit, 50)),
                )
            ).fetchall()
        results: list[MemoryRecord] = []
        for hit in hits:
            category = MemoryCategory(hit["category"])
            record = await self.get(category, int(hit["memory_id"]))
            if record:
                results.append(record)
        return results

    async def delete(self, category: MemoryCategory, memory_id: int) -> bool:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                f"DELETE FROM {category.value} WHERE id = ?", (memory_id,)
            )
            await db.execute(
                "DELETE FROM memory_fts WHERE memory_id = ? AND category = ?",
                (memory_id, category.value),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def set_importance(
        self, category: MemoryCategory, memory_id: int, importance: int
    ) -> bool:
        if not 1 <= importance <= 10:
            raise ValueError("importance must be between 1 and 10")
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                f"UPDATE {category.value} SET importance = ?, updated_at = ? WHERE id = ?",
                (importance, self._now(), memory_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def retain(self, max_short_term: int = 40, retention_days: int = 90) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            old_rows = await (
                await db.execute(
                    "SELECT id FROM short_term ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                    (max_short_term,),
                )
            ).fetchall()
            for (memory_id,) in old_rows:
                await db.execute("DELETE FROM short_term WHERE id = ?", (memory_id,))
                await db.execute(
                    "DELETE FROM memory_fts WHERE memory_id = ? AND category = 'short_term'",
                    (memory_id,),
                )
            for table in ("episodic", "homelab_events"):
                rows = await (
                    await db.execute(
                        f"SELECT id FROM {table} WHERE created_at < ? AND importance < 7", (cutoff,)
                    )
                ).fetchall()
                for (memory_id,) in rows:
                    await db.execute(f"DELETE FROM {table} WHERE id = ?", (memory_id,))
                    await db.execute(
                        "DELETE FROM memory_fts WHERE memory_id = ? AND category = ?",
                        (memory_id, table),
                    )
            await db.commit()

    async def get_state(self) -> str:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute("SELECT state FROM character_state WHERE singleton=1")).fetchone()
        return row[0] if row else "neutral"

    async def set_state(self, state: str) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "UPDATE character_state SET state=?, updated_at=? WHERE singleton=1",
                (state, self._now()),
            )
            await db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _record(category: MemoryCategory, row: aiosqlite.Row) -> MemoryRecord:
        keys = row.keys()
        return MemoryRecord(
            id=row["id"],
            category=category,
            content=row["content"],
            importance=row["importance"],
            role=row["role"] if "role" in keys else None,
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

