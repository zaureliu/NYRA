from __future__ import annotations

import random

from app.avatar import AvatarController
from app.events import Event, EventBus, EventType
from app.perception import PCAwareness
from app.proactive import ProactiveEngine
from app.realtime.models import CursorAttention, RealtimeConfig


class ReactionEngine:
    def __init__(self, event_bus: EventBus, avatar: AvatarController, perception: PCAwareness,
                 proactive: ProactiveEngine, config: RealtimeConfig, rng: random.Random | None = None) -> None:
        self.event_bus, self.avatar, self.perception = event_bus, avatar, perception
        self.proactive, self.config = proactive, config
        self.rng = rng or random.Random()
        self.last_reaction: dict = {}

    async def start(self) -> None:
        await self.event_bus.subscribe(self.handle_event)

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.handle_event)

    async def handle_event(self, event: Event) -> None:
        if event.type == EventType.PERCEPTION_UPDATED:
            await self._cursor_attention()
            return
        if event.type in {EventType.USER_SPEECH_STARTED, EventType.USER_SPEECH_RECEIVED}:
            await self._react("USER_CALLED_NYRA", visual="listening", expression="neutral", speak=False)
        elif event.type == EventType.LLM_STREAM_STARTED:
            await self._react("THINKING", visual="thinking", expression="focused", speak=False)
        elif event.type == EventType.PC_ACTIVE_WINDOW_CHANGED:
            chance = {"LOW": .02, "NORMAL": .05, "HIGH": .12}[self.config.reaction_frequency.value]
            if self.config.proactive_reactions and self.rng.random() < chance:
                await self._react("ACTIVE_APP_CHANGED", visual="idle", expression="curious", speak=False, app=event.payload.get("app"))
        elif event.type == EventType.USER_RETURNED:
            if self.config.proactive_reactions and self.proactive.allow("user_return_visual", priority=30, cooldown_seconds=600):
                await self._react("USER_RETURNED", visual="idle", expression="curious", speak=False)
        elif event.type == EventType.SYSTEM_LOAD_HIGH:
            await self._react("HIGH_CPU", visual="alert", expression="concerned", speak=False)
        elif event.type in {EventType.SENTINEL_ALERT, EventType.SENTINEL_EVENT}:
            severity = str(event.payload.get("severity") or event.payload.get("event", {}).get("severity") or "info")
            if severity in {"critical", "warning"}:
                await self._react("SENTINEL_CRITICAL" if severity == "critical" else "SENTINEL_WARNING", visual="alert", expression="concerned", speak=False, severity=severity)
        elif event.type == EventType.NETWORK_RECOVERED:
            await self._react("NETWORK_RECOVERY", visual="idle", expression="neutral", speak=False)

    async def _cursor_attention(self) -> None:
        if self.config.cursor_attention == CursorAttention.OFF or self.perception.snapshot.mouse.activity != "recent":
            return
        scale = .16 if self.config.cursor_attention == CursorAttention.SUBTLE else .32
        x = self.perception.snapshot.mouse.relative_x or 0
        y = self.perception.snapshot.mouse.relative_y or 0
        await self.avatar.update(eye_x=round(x * scale, 3), eye_y=round(y * scale, 3))

    async def _react(self, reaction: str, *, visual: str, expression: str, speak: bool, **context) -> None:
        await self.avatar.mode(visual, expression)
        self.last_reaction = {"reaction": reaction, "speech": speak, **context}
        await self.event_bus.publish(EventType.REACTION_TRIGGERED, **self.last_reaction)

    def status(self) -> dict:
        return self.last_reaction
