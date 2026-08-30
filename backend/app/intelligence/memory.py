from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.intelligence.models import MemoryItem, MemoryKind, MemoryWrite, Sensitivity
from app.intelligence.storage import IntelligenceStore
from app.intelligence.trust import contains_secret


WORDS = re.compile(r"[\wÀ-ÿ-]{2,}", re.UNICODE)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _loads(value: str | None, default):
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, ValueError):
        return default


class MemoryV2Service:
    """Selective, scored memory. Working memory remains process-local by design."""

    def __init__(self, store: IntelligenceStore, *, working_limit: int = 100) -> None:
        self.store = store
        self.working_limit = max(10, working_limit)
        self._working: dict[str, MemoryItem] = {}

    @staticmethod
    def should_persist(item: MemoryWrite) -> tuple[bool, str]:
        if item.kind == MemoryKind.WORKING:
            return False, "working_memory_is_ephemeral"
        if item.sensitivity in {Sensitivity.SECRET, Sensitivity.SENSITIVE}:
            return False, "sensitive_memory_requires_specialized_store"
        if contains_secret(item.content):
            return False, "secret_detected"
        if item.kind == MemoryKind.CONVERSATION and item.relevance < 0.75:
            return False, "conversation_not_salient"
        if item.confidence < 0.35 or item.relevance < 0.35:
            return False, "below_persistence_threshold"
        if len(item.content.strip()) < 4:
            return False, "content_too_short"
        return True, "eligible"

    async def write(self, item: MemoryWrite, *, force: bool = False) -> dict:
        if contains_secret(item.content) or item.sensitivity == Sensitivity.SECRET:
            raise PermissionError("MEMORY_SECRET_REJECTED")
        now = datetime.now(timezone.utc)
        memory_id = f"mem_{uuid4().hex}"
        if item.kind == MemoryKind.WORKING:
            record = MemoryItem(id=memory_id, created_at=now, updated_at=now, **item.model_dump())
            self._working[memory_id] = record
            self._trim_working(now)
            return {"status": "WRITTEN_EPHEMERAL", "memory": record.model_dump(mode="json"), "deduplicated": False}
        persist, reason = self.should_persist(item)
        if not persist and not force:
            return {"status": "SKIPPED", "reason": reason, "memory": None, "deduplicated": False}
        if item.sensitivity == Sensitivity.SENSITIVE:
            raise PermissionError("MEMORY_SENSITIVE_REQUIRES_OPT_IN")
        normalized = " ".join(item.content.split())
        content_hash = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()
        project = item.project or ""
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            existing = await (await db.execute(
                "SELECT * FROM memory_v2 WHERE kind=? AND project=? AND content_hash=?",
                (item.kind.value, project, content_hash),
            )).fetchone()
            if existing:
                confidence = max(float(existing["confidence"]), item.confidence)
                relevance = max(float(existing["relevance"]), item.relevance)
                await db.execute(
                    "UPDATE memory_v2 SET confidence=?, relevance=?, updated_at=?, metadata=? WHERE id=?",
                    (confidence, relevance, now.isoformat(), json.dumps(item.metadata, ensure_ascii=False), existing["id"]),
                )
                await db.commit()
                record = await self.get(str(existing["id"]))
                return {"status": "UPDATED", "reason": "deduplicated", "memory": record.model_dump(mode="json") if record else None, "deduplicated": True}

            conflict, conflict_group = await self._detect_conflict(db, item)
            await db.execute(
                """INSERT INTO memory_v2(
                    id,kind,content,content_hash,source,category,project,confidence,relevance,
                    sensitivity,expires_at,decay_half_life_days,provenance,related_entities,
                    metadata,conflict,conflict_group,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory_id, item.kind.value, normalized, content_hash, item.source,
                    item.category, project, item.confidence, item.relevance,
                    item.sensitivity.value, _iso(item.expires_at), item.decay_half_life_days,
                    json.dumps(item.provenance, ensure_ascii=False),
                    json.dumps(item.related_entities, ensure_ascii=False),
                    json.dumps(item.metadata, ensure_ascii=False), int(conflict), conflict_group,
                    now.isoformat(), now.isoformat(),
                ),
            )
            await db.execute("INSERT INTO memory_v2_fts(memory_id,content) VALUES(?,?)", (memory_id, normalized))
            await db.commit()
        record = await self.get(memory_id)
        return {"status": "WRITTEN", "reason": reason, "memory": record.model_dump(mode="json") if record else None, "deduplicated": False}

    async def get(self, memory_id: str) -> MemoryItem | None:
        if memory_id in self._working:
            self._trim_working(datetime.now(timezone.utc))
            return self._working.get(memory_id)
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM memory_v2 WHERE id=?", (memory_id,))).fetchone()
        return self._row(row) if row else None

    async def update(self, memory_id: str, *, content: str | None = None, confidence: float | None = None,
                     relevance: float | None = None, metadata: dict | None = None) -> MemoryItem | None:
        record = await self.get(memory_id)
        if not record:
            return None
        new_content = " ".join((content if content is not None else record.content).split())
        if contains_secret(new_content):
            raise PermissionError("MEMORY_SECRET_REJECTED")
        if record.kind == MemoryKind.WORKING:
            changed = record.model_copy(update={
                "content": new_content,
                "confidence": confidence if confidence is not None else record.confidence,
                "relevance": relevance if relevance is not None else record.relevance,
                "metadata": metadata if metadata is not None else record.metadata,
                "updated_at": datetime.now(timezone.utc),
            })
            self._working[memory_id] = changed
            return changed
        content_hash = hashlib.sha256(new_content.casefold().encode("utf-8")).hexdigest()
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                "UPDATE memory_v2 SET content=?,content_hash=?,confidence=?,relevance=?,metadata=?,updated_at=? WHERE id=?",
                (new_content, content_hash, confidence if confidence is not None else record.confidence,
                 relevance if relevance is not None else record.relevance,
                 json.dumps(metadata if metadata is not None else record.metadata, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(), memory_id),
            )
            await db.execute("DELETE FROM memory_v2_fts WHERE memory_id=?", (memory_id,))
            await db.execute("INSERT INTO memory_v2_fts(memory_id,content) VALUES(?,?)", (memory_id, new_content))
            await db.commit()
        return await self.get(memory_id)

    async def delete(self, memory_id: str) -> bool:
        if self._working.pop(memory_id, None) is not None:
            return True
        async with aiosqlite.connect(self.store.database_path) as db:
            cursor = await db.execute("DELETE FROM memory_v2 WHERE id=?", (memory_id,))
            await db.execute("DELETE FROM memory_v2_fts WHERE memory_id=?", (memory_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def retrieve(self, query: str, *, kinds: list[MemoryKind] | None = None,
                       project: str | None = None, limit: int = 8) -> list[MemoryItem]:
        now = datetime.now(timezone.utc)
        self._trim_working(now)
        terms = {word.casefold() for word in WORDS.findall(query)}
        candidates: dict[str, MemoryItem] = {}
        for record in self._working.values():
            if not kinds or record.kind in kinds:
                candidates[record.id] = record
        params: list[object] = []
        filters = ["(expires_at IS NULL OR expires_at > ?)"]
        params.append(now.isoformat())
        if kinds:
            filters.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(item.value for item in kinds)
        if project is not None:
            filters.append("project IN ('', ?)")
            params.append(project)
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT * FROM memory_v2 WHERE {' AND '.join(filters)} ORDER BY updated_at DESC LIMIT 300",
                params,
            )).fetchall()
        for row in rows:
            item = self._row(row)
            candidates[item.id] = item
        scored: list[MemoryItem] = []
        for item in candidates.values():
            words = {word.casefold() for word in WORDS.findall(item.content)}
            lexical = len(terms & words) / max(1, len(terms))
            age_days = max(0, (now - item.updated_at.astimezone(timezone.utc)).total_seconds() / 86400)
            decay = 1.0 if not item.decay_half_life_days else math.pow(0.5, age_days / item.decay_half_life_days)
            score = lexical * 0.5 + item.confidence * 0.2 + item.relevance * 0.2 + decay * 0.1
            if lexical > 0 or not terms:
                scored.append(item.model_copy(update={"score": round(score, 6)}))
        return sorted(scored, key=lambda item: (item.score, item.updated_at), reverse=True)[:max(1, min(limit, 50))]

    async def expire(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        before = len(self._working)
        self._trim_working(now)
        async with aiosqlite.connect(self.store.database_path) as db:
            ids = [row[0] for row in await (await db.execute(
                "SELECT id FROM memory_v2 WHERE expires_at IS NOT NULL AND expires_at <= ?", (now.isoformat(),)
            )).fetchall()]
            if ids:
                await db.executemany("DELETE FROM memory_v2_fts WHERE memory_id=?", [(item,) for item in ids])
                await db.executemany("DELETE FROM memory_v2 WHERE id=?", [(item,) for item in ids])
            await db.commit()
        return {"working": before - len(self._working), "persistent": len(ids)}

    async def _detect_conflict(self, db: aiosqlite.Connection, item: MemoryWrite) -> tuple[bool, str | None]:
        entities = {value.casefold() for value in item.related_entities if value.strip()}
        if not entities:
            return False, None
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id,content,related_entities FROM memory_v2 WHERE kind=? AND category=? AND project=? ORDER BY updated_at DESC LIMIT 100",
            (item.kind.value, item.category, item.project or ""),
        )).fetchall()
        current_words = {word.casefold() for word in WORDS.findall(item.content)}
        for row in rows:
            prior_entities = {str(value).casefold() for value in _loads(row["related_entities"], [])}
            if not entities.intersection(prior_entities):
                continue
            prior_words = {word.casefold() for word in WORDS.findall(row["content"])}
            similarity = len(current_words & prior_words) / max(1, len(current_words | prior_words))
            # Exact duplicates were handled by content_hash. Two materially
            # different assertions about the same entity/category are retained
            # and linked as a conflict instead of silently overwriting either.
            if similarity < 0.80:
                group = f"conflict_{hashlib.sha256('|'.join(sorted(entities)).encode()).hexdigest()[:16]}"
                await db.execute("UPDATE memory_v2 SET conflict=1,conflict_group=? WHERE id=?", (group, row["id"]))
                return True, group
        return False, None

    def _trim_working(self, now: datetime) -> None:
        expired = [key for key, item in self._working.items() if item.expires_at and item.expires_at <= now]
        for key in expired:
            self._working.pop(key, None)
        if len(self._working) > self.working_limit:
            ordered = sorted(self._working.values(), key=lambda item: item.updated_at, reverse=True)
            self._working = {item.id: item for item in ordered[:self.working_limit]}

    @staticmethod
    def _row(row: aiosqlite.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"], kind=MemoryKind(row["kind"]), content=row["content"], source=row["source"],
            category=row["category"], project=row["project"] or None, confidence=float(row["confidence"]),
            relevance=float(row["relevance"]), sensitivity=Sensitivity(row["sensitivity"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            decay_half_life_days=row["decay_half_life_days"], provenance=_loads(row["provenance"], {}),
            related_entities=_loads(row["related_entities"], []), metadata=_loads(row["metadata"], {}),
            conflict=bool(row["conflict"]), conflict_group=row["conflict_group"],
            created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]),
        )
