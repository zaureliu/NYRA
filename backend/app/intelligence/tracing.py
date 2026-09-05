from __future__ import annotations

import json
import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from app.intelligence.models import TraceEntry, TraceStage
from app.intelligence.storage import IntelligenceStore
from app.intelligence.trust import redact


class TraceService:
    def __init__(self, store: IntelligenceStore) -> None:
        self.store = store
        self._sequences: defaultdict[str, int] = defaultdict(int)
        self._sequence_lock = asyncio.Lock()

    def new(self) -> str:
        return f"trace_{uuid4().hex}"

    async def record(self, trace_id: str, stage: TraceStage, *, component: str,
                     operation: str, payload: dict[str, Any] | None = None,
                     correlation_id: str | None = None, task_id: str | None = None,
                     severity: str = "INFO", duration_ms: float | None = None) -> TraceEntry:
        async with self._sequence_lock:
            async with aiosqlite.connect(self.store.database_path) as db:
                sequence = self._sequences.get(trace_id)
                if sequence is None:
                    row = await (await db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM execution_traces WHERE trace_id=?",
                        (trace_id,),
                    )).fetchone()
                    sequence = int(row[0]) if row else 0
                sequence += 1
                entry = TraceEntry(
                    trace_id=trace_id, sequence=sequence, stage=stage,
                    component=component, operation=operation, payload=redact(payload or {}),
                    correlation_id=correlation_id, task_id=task_id, severity=severity,
                    duration_ms=duration_ms,
                )
                await db.execute(
                    """INSERT INTO execution_traces(trace_id,sequence,timestamp,stage,component,operation,
                       correlation_id,task_id,severity,duration_ms,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (entry.trace_id, entry.sequence, entry.timestamp.isoformat(), entry.stage.value,
                     entry.component, entry.operation, entry.correlation_id, entry.task_id,
                     entry.severity, entry.duration_ms, json.dumps(entry.payload, ensure_ascii=False)),
                )
                await db.commit()
                self._sequences[trace_id] = sequence
                if len(self._sequences) > 5_000:
                    self._sequences.pop(next(iter(self._sequences)))
        return entry

    async def get(self, trace_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM execution_traces WHERE trace_id=? ORDER BY sequence", (trace_id,)
            )).fetchall()
        return [{key: json.loads(row[key]) if key == "payload" else row[key] for key in row.keys()} for row in rows]

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM execution_traces ORDER BY timestamp DESC LIMIT ?", (min(limit, 500),)
            )).fetchall()
        return [{key: json.loads(row[key]) if key == "payload" else row[key] for key in row.keys()} for row in rows]

    async def replay(self, trace_id: str, *, dry_run: bool = True) -> dict[str, Any]:
        entries = await self.get(trace_id)
        if not entries:
            raise KeyError("TRACE_NOT_FOUND")
        replayed = []
        skipped = []
        for entry in entries:
            if entry["stage"] in {TraceStage.TOOL_CALL.value, TraceStage.TOOL_RESULT.value}:
                skipped.append({"sequence": entry["sequence"], "reason": "external_actions_never_replayed_blindly"})
                continue
            replayed.append({"sequence": entry["sequence"], "stage": entry["stage"], "operation": entry["operation"]})
        return {"trace_id": trace_id, "mode": "DRY_RUN" if dry_run else "SAFE_NON_ACTION_REPLAY",
                "replayed": replayed, "skipped": skipped, "destructive_actions": 0}


class RuntimeTraceObserver:
    """Bounded EventBus-to-trace bridge for real runtime executions."""

    def __init__(self, event_bus, traces: TraceService, *, queue_size: int = 1000) -> None:
        self.event_bus = event_bus
        self.traces = traces
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(100, queue_size))
        self._worker: asyncio.Task | None = None
        self._by_correlation: dict[str, str] = {}
        self.dropped = 0
        self.persist_failures = 0
        self.last_error: str | None = None

    async def start(self) -> None:
        await self.event_bus.subscribe(self.observe)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="kazumi-runtime-traces")

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.observe)
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def observe(self, event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._record_event(event)
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - tracing must never stop runtime execution
                self.persist_failures += 1
                self.last_error = type(error).__name__
            finally:
                self.queue.task_done()

    async def _record_event(self, event) -> None:
        name = event.type.value
        payload = redact(event.payload)
        correlation = str(
            payload.get("turn_id") or payload.get("agent_run_id")
            or payload.get("conversation_id") or event.id
        )
        stage = self._stage(name)
        if stage is None:
            return
        trace_id = self._by_correlation.get(correlation)
        if trace_id is None:
            trace_id = self.traces.new()
            self._by_correlation[correlation] = trace_id
            if len(self._by_correlation) > 1000:
                self._by_correlation.pop(next(iter(self._by_correlation)))
        await self.traces.record(
            trace_id, stage, component="event_bus", operation=name,
            payload={"event_id": event.id, "data": payload}, correlation_id=correlation,
            task_id=str(payload.get("task_id") or "") or None,
            severity="ERROR" if any(part in name.casefold() for part in ("failed", "error", "crash")) else "INFO",
        )

    @staticmethod
    def _stage(name: str) -> TraceStage | None:
        if name in {"USER_TEXT_RECEIVED", "USER_SPEECH_RECEIVED"}:
            return TraceStage.USER_REQUEST
        if name == "LLM_PROCESSING":
            return TraceStage.CONTEXT_ASSEMBLY
        if name in {"AGENT_RUN_STARTED", "TASK_STARTED", "WORKFLOW_TRIGGERED"}:
            return TraceStage.PLAN
        if name.endswith("APPROVAL_REQUIRED") or name.endswith("APPROVAL_DECIDED"):
            return TraceStage.POLICY_DECISIONS
        if name.endswith("EXECUTION_STARTED"):
            return TraceStage.TOOL_CALL
        if name.endswith("EXECUTION_FINISHED") or name in {"AGENT_RUN_STEP", "TASK_FINISHED", "WORKFLOW_FINISHED"}:
            return TraceStage.TOOL_RESULT
        if "VERIFIED" in name or "VALIDATION" in name or name.endswith("HEALTH_PASSED"):
            return TraceStage.VERIFICATION
        if name in {"AGENT_RUN_FINISHED", "AGENT_RUN_CANCELLED"} or name.endswith("FAILED"):
            return TraceStage.FINAL_DECISION
        if name == "KAZUMI_RESPONSE":
            return TraceStage.RESPONSE
        return None
