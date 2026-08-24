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

    def status(self) -> dict:
        return self.state.model_dump(mode="json")
