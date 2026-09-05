from __future__ import annotations

import asyncio
from pydantic import BaseModel, Field

from app.events import EventBus, EventType


class AvatarState(BaseModel):
    expression: str = "neutral"
    expression_weight: float = Field(1, ge=0, le=1)
    eye_x: float = Field(0, ge=-1, le=1)
    eye_y: float = Field(0, ge=-1, le=1)
    head_x: float = Field(0, ge=-1, le=1)
    head_y: float = Field(0, ge=-1, le=1)
    head_tilt: float = Field(0, ge=-1, le=1)
    body_x: float = Field(0, ge=-1, le=1)
    breathing: float = Field(0.5, ge=0, le=1)
    mouth_open: float = Field(0, ge=0, le=1)
    neural_link: str = "idle"
    animation: str = "idle"
    emotion_intensity: float = Field(0, ge=0, le=.65)
    transition_ms: int = Field(0, ge=0, le=5000)
    transition_ease: str = "ease-out"
    minimum_hold_ms: int = Field(0, ge=0, le=60000)
    presentation_cooldown_ms: int = Field(0, ge=0, le=60000)


class AvatarController:
    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self.state = AvatarState()
        self._lock = asyncio.Lock()
        self.provider = None

    def attach_provider(self, provider) -> None:
        self.provider = provider

    async def update(self, **values) -> AvatarState:
        async with self._lock:
            self.state = self.state.model_copy(update=values)
            self.state = AvatarState.model_validate(self.state.model_dump())
        await self.event_bus.publish(EventType.AVATAR_STATE_CHANGED, **self.state.model_dump(mode="json"))
        if self.provider is not None:
            await self.provider.apply(self.state)
        return self.state

    async def mode(self, name: str, expression: str | None = None) -> AvatarState:
        presets = {
            "idle": {"head_tilt": 0, "neural_link": "idle", "animation": "idle"},
            "listening": {"head_tilt": 0, "neural_link": "listening", "animation": "listening"},
            "thinking": {"head_tilt": 0.12, "eye_x": 0.16, "neural_link": "thinking", "animation": "thinking"},
            "speaking": {"head_tilt": 0, "neural_link": "speaking", "animation": "speaking"},
            "alert": {"head_tilt": -0.08, "neural_link": "alert", "animation": "attention"},
        }
        values = dict(presets.get(name, presets["idle"]))
        if expression:
            values["expression"] = expression
        return await self.update(**values)

    async def apply_emotion(self, emotion: str, intensity: float, transition: dict) -> AvatarState:
        """Apply canonical emotion without changing the operational animation."""
        expressions = {
            "neutral": "neutral", "friendly": "slight_smile",
            "focused": "focused", "confident": "slight_smile",
            "positive": "slight_smile", "happy": "happy",
            "relieved": "slight_smile", "concerned": "concerned",
            "warning": "focused", "serious": "focused",
            "empathetic": "concerned", "curious": "curious",
            "surprised": "surprised", "amused": "amused",
            "apologetic": "concerned", "uncertain": "concerned",
            "calm": "neutral",
        }
        return await self.update(
            expression=expressions.get(emotion, "neutral"),
            expression_weight=min(.65, max(0.0, float(intensity))),
            emotion_intensity=min(.65, max(0.0, float(intensity))),
            transition_ms=int(transition.get("transition_ms") or 0),
            transition_ease=str(transition.get("ease") or "ease-out"),
            minimum_hold_ms=int(transition.get("minimum_hold_ms") or 0),
            presentation_cooldown_ms=int(transition.get("cooldown_ms") or 0),
        )

    def status(self) -> dict:
        return self.state.model_dump(mode="json")
