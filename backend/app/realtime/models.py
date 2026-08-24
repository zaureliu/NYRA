from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.conversation.models import ConversationState as RealtimeStatus


class DuplexMode(StrEnum):
    HALF_DUPLEX = "HALF_DUPLEX"
    SMART_DUPLEX = "SMART_DUPLEX"


class CursorAttention(StrEnum):
    OFF = "OFF"
    SUBTLE = "SUBTLE"
    ACTIVE = "ACTIVE"


class ReactionFrequency(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class RealtimeConfig(BaseModel):
    streaming_responses: bool = True
    sentence_streaming: bool = True
    duplex_mode: DuplexMode = DuplexMode.HALF_DUPLEX
    barge_in: bool = False
    minimum_chunk_characters: int = Field(28, ge=8, le=240)
    minimum_chunk_words: int = Field(4, ge=1, le=40)
    chunk_timeout_ms: int = Field(850, ge=150, le=5000)
    perception_enabled: bool = True
    cursor_attention: CursorAttention = CursorAttention.SUBTLE
    active_app_awareness: bool = True
    proactive_reactions: bool = True
    reaction_frequency: ReactionFrequency = ReactionFrequency.NORMAL


class PrivacyConfig(BaseModel):
    active_app: bool = True
    window_title: bool = False
    mouse_position: bool = True
    idle_detection: bool = True
    system_metrics: bool = True
    screen_capture: bool = False

    @field_validator("screen_capture")
    @classmethod
    def screen_capture_not_available(cls, value: bool) -> bool:
        if value:
            raise ValueError("Screen Capture permanece OFF e não implementado na V4")
        return False


class RealtimeSettingsUpdate(BaseModel):
    realtime: RealtimeConfig
    privacy: PrivacyConfig
