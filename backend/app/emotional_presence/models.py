"""Typed contracts for synchronized emotional presentation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.persona_runtime.models import KazumiEmotion


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VoiceEmotionSupport(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class EmotionTransition(BaseModel):
    previous: KazumiEmotion
    emotion: KazumiEmotion
    intensity: float = Field(ge=0.0, le=0.65)
    transition_ms: int = Field(ge=0, le=5000)
    ease: str = Field(pattern=r"^(linear|ease|ease-in|ease-out|ease-in-out)$")
    minimum_hold_ms: int = Field(ge=0, le=60000)
    cooldown_ms: int = Field(ge=0, le=60000)
    reason: str = Field(max_length=180)


class VoiceStylePresentation(BaseModel):
    emotion: KazumiEmotion
    intensity: float = Field(ge=0.0, le=0.65)
    provider: str
    voice_identity: str
    delivery: str
    acoustic_emotion: str
    emotion_support: VoiceEmotionSupport
    native_controls: list[str] = Field(default_factory=list)
    speaking_rate: float = Field(ge=0.7, le=1.3)
    pitch_adjustment_hz: int = Field(default=0, ge=-20, le=20)
    style_instruction: str = Field(default="", max_length=500)
    capabilities: dict[str, bool] = Field(default_factory=dict)
    degraded: bool = False
    degradation_reason: str | None = None


class AvatarEmotionPresentation(BaseModel):
    emotion: KazumiEmotion
    intensity: float = Field(ge=0.0, le=0.65)
    state_expression: str
    vts_kind: Literal["hotkey", "expression", "parameter", "neutral", "offline", "disabled"]
    vts_target: str | None = None
    vts_applied: bool = False
    fallback: str | None = None
    model_id: str | None = None


class EmotionalPresenceSettings(BaseModel):
    enabled: bool = True
    voice_expression: bool = True
    avatar_expression: bool = True

    model_config = {"extra": "forbid"}


class EmotionalPresenceSettingsUpdate(BaseModel):
    enabled: bool | None = None
    voice_expression: bool | None = None
    avatar_expression: bool | None = None

    model_config = {"extra": "forbid"}


class EmotionalPresentationSnapshot(BaseModel):
    state: str = "READY"
    source: str = "persona_runtime"
    emotion: KazumiEmotion
    intensity: float = Field(ge=0.0, le=0.65)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    transition: EmotionTransition
    voice: VoiceStylePresentation | None = None
    avatar: AvatarEmotionPresentation | None = None
    synchronized_at: datetime = Field(default_factory=utc_now)
