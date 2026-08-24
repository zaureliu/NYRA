from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.config import Settings
from app.events import EventBus, EventType
from app.memory import MemoryRepository
from app.memory.models import MemoryCategory, MemoryCreate
from app.tools import ToolRegistry


logger = logging.getLogger("nyra.homelab")


class HomelabMonitor:
    def __init__(
        self,
        settings: Settings,
        tools: ToolRegistry,
        event_bus: EventBus,
        memory: MemoryRepository,
    ) -> None:
        self.settings = settings
        self.tools = tools
        self.event_bus = event_bus
        self.memory = memory
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._cooldowns: dict[str, float] = {}
        self.last_stats: dict[str, Any] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="nyra-homelab-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def poll_once(self) -> list[dict[str, Any]]:
        result = await self.tools.execute("get_local_system_stats", {})
        if not result.ok:
            return []
        self.last_stats = result.data
        events: list[dict[str, Any]] = []
        checks = (
            ("high_cpu", result.data.get("cpu_percent", 0), self.settings.cpu_alert_threshold, "CPU"),
            ("high_memory", result.data.get("memory_percent", 0), self.settings.memory_alert_threshold, "RAM"),
        )
        for key, value, threshold, label in checks:
            if value >= threshold and self._cooldown_ready(key):
                event = {
                    "key": key,
                    "severity": "warning",
                    "message": f"{label} em {value:.1f}% (limite {threshold:.1f}%)",
                    "value": value,
                    "threshold": threshold,
                    "proactive": self.settings.proactive_mode,
                }
                events.append(event)
                await self.event_bus.publish(EventType.HOMELAB_EVENT, **event)
                await self.memory.add(
                    MemoryCreate(
                        category=MemoryCategory.HOMELAB_EVENTS,
                        content=event["message"],
                        importance=7,
                        metadata={"key": key},
                    )
                )
                logger.warning("homelab_threshold", extra=event)
        return events

    def _cooldown_ready(self, key: str) -> bool:
        now = time.monotonic()
        previous = self._cooldowns.get(key, 0)
        if now - previous < self.settings.event_cooldown_seconds:
            return False
        self._cooldowns[key] = now
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                logger.exception("monitor_poll_failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.homelab_poll_interval
                )
            except TimeoutError:
                continue
