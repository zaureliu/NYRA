from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time

import psutil

from app.events import EventBus, EventType
from app.perception.models import PerceptionSnapshot, SystemSnapshot
from app.perception.windows import foreground_app, idle_seconds, mouse_state, virtual_resolution
from app.realtime.models import PrivacyConfig, RealtimeConfig


class PCAwareness:
    """Low-frequency, local-only PC sensors. It never captures keys, clipboard or pixels."""

    def __init__(self, event_bus: EventBus, realtime: RealtimeConfig, privacy: PrivacyConfig) -> None:
        self.event_bus = event_bus
        self.realtime = realtime
        self.privacy = privacy
        self.snapshot = PerceptionSnapshot(enabled=False)
        self._task: asyncio.Task[None] | None = None
        self._last_mouse: tuple[float | None, float | None] = (None, None)
        self._last_mouse_motion = 0.0
        self._last_idle_state = "unknown"
        self._high_load_since: float | None = None
        self._high_load_emitted = False

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="nyra-pc-awareness")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.snapshot.enabled = False

    async def update(self, realtime: RealtimeConfig, privacy: PrivacyConfig) -> None:
        self.realtime, self.privacy = realtime, privacy
        if realtime.perception_enabled:
            await self.start()
        else:
            await self.stop()
            self.snapshot = PerceptionSnapshot(enabled=False)

    async def _run(self) -> None:
        psutil.cpu_percent(None)
        while True:
            if self.realtime.perception_enabled:
                await self.poll_once()
            else:
                self.snapshot = PerceptionSnapshot(enabled=False)
            await asyncio.sleep(0.5)

    async def poll_once(self) -> PerceptionSnapshot:
        previous = self.snapshot
        app_enabled = self.privacy.active_app and self.realtime.active_app_awareness
        app = await asyncio.to_thread(foreground_app, self.privacy.window_title) if app_enabled else previous.foreground_app.model_copy(update={"process": "disabled", "classification": "Disabled", "title": None})
        idle = await asyncio.to_thread(idle_seconds) if self.privacy.idle_detection else 0.0
        activity = "long_idle" if idle >= 900 else "idle" if idle >= 300 else "active"
        mouse = await asyncio.to_thread(mouse_state, self.privacy.mouse_position)
        now = time.monotonic()
        current_mouse = (mouse.relative_x, mouse.relative_y)
        if current_mouse != self._last_mouse:
            self._last_mouse_motion = now
            self._last_mouse = current_mouse
        mouse.activity = "recent" if now - self._last_mouse_motion <= 2 else "still"
        if self.privacy.system_metrics:
            memory = psutil.virtual_memory()
            system = SystemSnapshot(
                cpu_percent=psutil.cpu_percent(None), ram_percent=memory.percent,
                disk_percent=psutil.disk_usage("/").percent, resolution=virtual_resolution(),
            )
        else:
            system = SystemSnapshot()
        self.snapshot = PerceptionSnapshot(
            timestamp=datetime.now(timezone.utc), enabled=True, user_activity=activity,
            idle_seconds=round(idle, 1), foreground_app=app, mouse=mouse, system=system,
            network=previous.network, sentinel=previous.sentinel, nyra_state=previous.nyra_state,
        )
        if app.process != previous.foreground_app.process:
            await self.event_bus.publish(EventType.PC_ACTIVE_WINDOW_CHANGED, process=app.process, app=app.classification)
        if mouse.activity != previous.mouse.activity:
            await self.event_bus.publish(EventType.MOUSE_ACTIVITY_CHANGED, activity=mouse.activity)
        if activity != self._last_idle_state:
            if activity in {"idle", "long_idle"}:
                await self.event_bus.publish(EventType.USER_IDLE, duration_seconds=round(idle, 1), level=activity)
            elif self._last_idle_state in {"idle", "long_idle"}:
                await self.event_bus.publish(EventType.USER_RETURNED, idle_seconds=round(previous.idle_seconds, 1))
            self._last_idle_state = activity
        await self._check_load(system.cpu_percent)
        await self.event_bus.publish(
            EventType.PERCEPTION_UPDATED, app=app.classification, user_activity=activity,
            cpu=round(system.cpu_percent, 1), ram=round(system.ram_percent, 1),
        )
        return self.snapshot

    async def _check_load(self, cpu: float) -> None:
        now = time.monotonic()
        if cpu >= 90:
            self._high_load_since = self._high_load_since or now
            if not self._high_load_emitted and now - self._high_load_since >= 10:
                self._high_load_emitted = True
                await self.event_bus.publish(EventType.SYSTEM_LOAD_HIGH, cpu=round(cpu, 1), sustained_seconds=10)
        else:
            self._high_load_since = None
            self._high_load_emitted = False

    def public_snapshot(self) -> dict:
        value = self.snapshot.model_dump(mode="json")
        if not self.privacy.window_title:
            value["foreground_app"]["title"] = None
        if not self.privacy.mouse_position:
            value["mouse"]["relative_x"] = None
            value["mouse"]["relative_y"] = None
        return value
