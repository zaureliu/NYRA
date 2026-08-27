from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.events import Event, EventBus, EventType
from app.selfdev.storage import atomic_write_json


WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


class RuntimeObserver:
    """Aggregates numeric telemetry only; conversation bodies are never stored."""

    EVENT_METRICS: dict[EventType, str] = {
        EventType.ERROR: "unhandled_exception_count",
        EventType.TTS_CHUNK_FAILED: "tts_order_violation_count",
        EventType.TTS_FAILED: "tts_order_violation_count",
        EventType.RUNTIME_RESTARTING: "backend_restart_count",
        EventType.RUNTIME_HEALTH_FAILED: "tool_verification_failure_rate",
        EventType.DESKTOP_WINDOW_VERIFIED: "desktop_launch_verification_count",
        EventType.COMPUTER_VERIFICATION_FAILURE: "tool_verification_failure_rate",
        EventType.REMOTE_SHELL_EXECUTION_FINISHED: "remote_shell_completion_count",
        EventType.SENTINEL_STATUS_CHANGED: "satellite_status_change_count",
    }

    def __init__(self, event_bus: EventBus, snapshot_path: Path) -> None:
        self.event_bus = event_bus
        self.snapshot_path = snapshot_path
        self._metrics: dict[str, deque[tuple[datetime, float]]] = defaultdict(lambda: deque(maxlen=20_000))
        self._started = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if not self._started:
            await self.event_bus.subscribe(self.observe_event)
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self.event_bus.unsubscribe(self.observe_event)
            self._started = False
        self.persist()

    async def observe_event(self, event: Event) -> None:
        metric = self.EVENT_METRICS.get(event.type)
        if metric:
            await self.record(metric, 1)
        if event.type == EventType.LLM_STREAM_STARTED:
            value = event.payload.get("ttft_ms")
            if isinstance(value, (int, float)):
                await self.record("simple_chat_ttft", float(value))
        elapsed = event.payload.get("elapsed_ms")
        if isinstance(elapsed, (int, float)):
            if event.type in {EventType.SHELL_EXECUTION_FINISHED, EventType.REMOTE_SHELL_EXECUTION_FINISHED}:
                await self.record("tool_latency_ms", float(elapsed))

    async def record(self, metric: str, value: float = 1.0, *, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(timezone.utc)
        async with self._lock:
            series = self._metrics[metric]
            series.append((timestamp, float(value)))
            cutoff = timestamp - WINDOWS["7d"]
            while series and series[0][0] < cutoff:
                series.popleft()

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        output: dict[str, Any] = {}
        for name, series in self._metrics.items():
            windows: dict[str, Any] = {}
            for label, duration in WINDOWS.items():
                values = [value for timestamp, value in series if timestamp >= current - duration]
                windows[label] = {
                    "count": len(values),
                    "sum": round(sum(values), 4),
                    "average": round(sum(values) / len(values), 4) if values else None,
                    "maximum": max(values) if values else None,
                }
            output[name] = windows
        return {"generated_at": current.isoformat(), "metrics": output}

    def persist(self) -> None:
        atomic_write_json(self.snapshot_path, self.snapshot())
