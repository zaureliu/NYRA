from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time

from app.events import Event, EventBus, EventType


@dataclass
class AttentionState:
    source: str = "neutral"
    priority: int = 0
    expires_at: float = 0


class AttentionEngine:
    PRIORITY = {
        EventType.USER_SPEECH_STARTED: ("user", 100, 8),
        EventType.USER_SPEECH_FINAL: ("conversation", 100, 12),
        EventType.SENTINEL_EVENT: ("sentinel", 90, 12),
        EventType.NETWORK_ALERT: ("network", 90, 10),
        EventType.SYSTEM_LOAD_HIGH: ("system", 70, 8),
        EventType.PC_ACTIVE_WINDOW_CHANGED: ("active_app", 40, 3),
        EventType.USER_IDLE: ("idle", 20, 5),
    }

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self.current = AttentionState()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.event_bus.subscribe(self.handle_event)
        self._task = asyncio.create_task(self._decay(), name="nyra-attention-decay")

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.handle_event)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def handle_event(self, event: Event) -> None:
        rule = self.PRIORITY.get(event.type)
        if not rule:
            return
        source, priority, ttl = rule
        severity = str(event.payload.get("severity", "")).casefold()
        if event.type == EventType.SENTINEL_EVENT and severity not in {"critical", "warning"}:
            priority = 50
        if priority < self.current.priority and self.current.expires_at > time.monotonic():
            return
        previous = self.current.source
        self.current = AttentionState(source, priority, time.monotonic() + ttl)
        await self.event_bus.publish(EventType.ATTENTION_CHANGED, previous=previous, current=source, priority=priority)

    async def _decay(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.current.source != "neutral" and self.current.expires_at <= time.monotonic():
                previous = self.current.source
                self.current = AttentionState()
                await self.event_bus.publish(EventType.ATTENTION_CHANGED, previous=previous, current="neutral", priority=0)

    def status(self) -> dict:
        return {"source": self.current.source, "priority": self.current.priority, "remaining_seconds": round(max(0, self.current.expires_at - time.monotonic()), 1)}
