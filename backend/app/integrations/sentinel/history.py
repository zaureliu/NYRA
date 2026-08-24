from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.integrations.sentinel.models import SentinelEvent


class SentinelHistory:
    def __init__(self, database_path: Path, retention_days: int = 30) -> None:
        self.database_path = database_path
        self.retention_days = retention_days

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS sentinel_events (
                event_id TEXT PRIMARY KEY,
                source_instance TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                entity TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}',
                spoken INTEGER NOT NULL DEFAULT 0,
                received_at TEXT NOT NULL
            )""")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_events_timestamp ON sentinel_events(timestamp)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sentinel_events_severity ON sentinel_events(severity)")
            await db.commit()

    async def add(self, event: SentinelEvent) -> bool:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO sentinel_events(
                    event_id,source_instance,timestamp,type,severity,title,summary,entity,metadata,received_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id, event.instance_id, event.timestamp.isoformat(), event.type,
                    event.severity.value, event.title, event.summary,
                    json.dumps(event.entity.model_dump(mode="json"), ensure_ascii=False),
                    json.dumps(event.metadata, ensure_ascii=False), datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_spoken(self, event_id: str) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("UPDATE sentinel_events SET spoken=1 WHERE event_id=?", (event_id,))
            await db.commit()

    async def recent(self, hours: int = 24, limit: int = 50, severity: str = "") -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat()
        query = "SELECT * FROM sentinel_events WHERE timestamp >= ?"
        parameters: list[object] = [cutoff]
        if severity:
            query += " AND severity = ?"
            parameters.append(severity)
        query += " ORDER BY timestamp DESC LIMIT ?"
        parameters.append(max(1, min(limit, 200)))
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(query, parameters)).fetchall()
        return [self._row(row) for row in rows]

    async def search(self, query: str, hours: int = 720, limit: int = 50, severity: str = "") -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat()
        sql = "SELECT * FROM sentinel_events WHERE timestamp >= ? AND (title LIKE ? OR summary LIKE ? OR entity LIKE ?)"
        needle = f"%{query[:120]}%"
        parameters: list[object] = [cutoff, needle, needle, needle]
        if severity:
            sql += " AND severity = ?"
            parameters.append(severity)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        parameters.append(max(1, min(limit, 200)))
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(sql, parameters)).fetchall()
        return [self._row(row) for row in rows]

    async def summary(self, hours: int = 1) -> dict:
        events = await self.recent(hours, 200)
        return {
            "hours": hours,
            "events": len(events),
            "info": sum(item["severity"] == "info" for item in events),
            "warnings": sum(item["severity"] == "warning" for item in events),
            "critical": sum(item["severity"] == "critical" for item in events),
            "recoveries": sum(item["severity"] == "recovery" for item in events),
            "latest": events[:5],
        }

    async def cleanup(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM sentinel_events WHERE timestamp < ?", (cutoff,))
            await db.commit()

    @staticmethod
    def _row(row) -> dict:
        value = dict(row)
        value["entity"] = json.loads(value.get("entity") or "{}")
        value["metadata"] = json.loads(value.get("metadata") or "{}")
        value["spoken"] = bool(value.get("spoken"))
        return value
