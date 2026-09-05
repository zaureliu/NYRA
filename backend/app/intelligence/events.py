from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from app.events import Event
from app.intelligence.models import EvidenceLevel, IntelligenceEvent
from app.intelligence.storage import IntelligenceStore
from app.intelligence.trust import redact


class EventIntelligenceEngine:
    NETWORK_MARKERS = ("network", "openwrt", "sentinel", "proxmox", "packet", "gateway")
    COALESCED_CATEGORIES = {
        "PERCEPTION_UPDATED", "MOUSE_ACTIVITY_CHANGED", "PC_ACTIVE_WINDOW_CHANGED",
        "COMPUTER_STATE_UPDATED", "NETWORK_STATUS_UPDATED",
    }

    def __init__(self, store: IntelligenceStore, event_bus, *, queue_size: int = 1000,
                 correlation_window_seconds: int = 180) -> None:
        self.store = store
        self.event_bus = event_bus
        self.queue: asyncio.Queue[IntelligenceEvent] = asyncio.Queue(maxsize=max(100, queue_size))
        self.window = timedelta(seconds=max(10, correlation_window_seconds))
        self._recent: deque[IntelligenceEvent] = deque(maxlen=500)
        self._worker: asyncio.Task | None = None
        self.dropped = 0
        self.coalesced = 0
        self.persist_failures = 0
        self.last_error: str | None = None
        self._last_observed: dict[tuple[str, str], float] = {}

    async def start(self) -> None:
        await self.event_bus.subscribe(self.observe)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="nyra-event-intelligence")

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.observe)
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def observe(self, event: Event) -> None:
        if event.type.value == "TTS_PCM_CHUNK":
            return  # transient playback audio is never an intelligence record
        payload = redact(event.payload)
        type_value = event.type.value
        severity = "ERROR" if any(term in type_value.casefold() for term in ("fail", "error", "offline", "crash")) else "WARNING" if "degraded" in type_value.casefold() else "INFO"
        entity = next((str(payload.get(key)) for key in ("entity", "host", "service", "app", "resource") if payload.get(key)), None)
        if type_value in self.COALESCED_CATEGORIES:
            key = (type_value, (entity or "").casefold())
            now = time.monotonic()
            if now - self._last_observed.get(key, 0) < 10:
                self.coalesced += 1
                return
            self._last_observed[key] = now
        correlation = str(payload.get("correlation_id") or payload.get("turn_id") or "") or None
        item = IntelligenceEvent(source="event_bus", category=type_value, severity=severity,
                                 entity=entity, payload=payload, correlation_id=correlation)
        self._correlate(item)
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1

    async def ingest(self, item: IntelligenceEvent) -> IntelligenceEvent:
        safe = item.model_copy(update={"payload": redact(item.payload)})
        self._correlate(safe)
        await self._persist(safe)
        return safe

    def _correlate(self, item: IntelligenceEvent) -> None:
        cutoff = item.timestamp - self.window
        while self._recent and self._recent[0].timestamp < cutoff:
            self._recent.popleft()
        related = []
        category = item.category.casefold()
        for prior in self._recent:
            same_entity = bool(
                item.entity and prior.entity
                and item.entity.casefold() == prior.entity.casefold()
                and (item.severity != "INFO" or prior.severity != "INFO")
            )
            network_related = any(marker in category for marker in self.NETWORK_MARKERS) and any(marker in prior.category.casefold() for marker in self.NETWORK_MARKERS)
            if same_entity or network_related:
                related.append(prior)
        if related:
            correlation_id = related[0].correlation_id or f"corr_{uuid4().hex}"
            item.correlation_id = correlation_id
            item.evidence_level = EvidenceLevel.CORRELATED
            item.confidence = min(0.95, 0.55 + len(related) * 0.08)
            for prior in related:
                if prior.correlation_id is None:
                    prior.correlation_id = correlation_id
        self._recent.append(item)

    async def recent(self, *, limit: int = 100, correlation_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE correlation_id=?" if correlation_id else ""
        params = (correlation_id, min(limit, 500)) if correlation_id else (min(limit, 500),)
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT * FROM intelligence_events {where} ORDER BY timestamp DESC LIMIT ?", params
            )).fetchall()
        return [{key: row[key] if key != "payload" else json.loads(row[key]) for key in row.keys()} for row in rows]

    async def incidents(self, *, limit: int = 30) -> list[dict[str, Any]]:
        events = await self.recent(limit=500)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event.get("correlation_id"):
                grouped.setdefault(str(event["correlation_id"]), []).append(event)
        return [{"correlation_id": key, "events": value[:50], "evidence_level": "CORRELATED",
                 "confidence": max(float(item.get("confidence") or 0) for item in value),
                 "causality_confirmed": False} for key, value in list(grouped.items())[:limit]]

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                await self._persist(item)
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - one bad write cannot kill correlation
                self.persist_failures += 1
                self.last_error = type(error).__name__
            finally:
                self.queue.task_done()

    async def _persist(self, item: IntelligenceEvent) -> None:
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO intelligence_events(event_id,timestamp,source,category,severity,entity,payload,
                   correlation_id,evidence_level,confidence) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item.event_id, item.timestamp.isoformat(), item.source, item.category, item.severity, item.entity,
                 json.dumps(redact(item.payload), ensure_ascii=False), item.correlation_id, item.evidence_level.value, item.confidence),
            )
            if item.correlation_id:
                related_ids = [
                    prior.event_id for prior in self._recent
                    if prior.correlation_id == item.correlation_id and prior.event_id != item.event_id
                ]
                if related_ids:
                    await db.executemany(
                        "UPDATE intelligence_events SET correlation_id=?, evidence_level='CORRELATED' WHERE event_id=?",
                        [(item.correlation_id, event_id) for event_id in related_ids],
                    )
            await db.commit()
