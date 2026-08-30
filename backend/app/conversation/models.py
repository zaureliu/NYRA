from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ConversationState(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    USER_SPEAKING = "USER_SPEAKING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ERROR = "ERROR"


class InterruptionTarget(StrEnum):
    SPEECH = "speech"
    TASK = "task"


class AudioSettingsUpdate(BaseModel):
    microphone: str = Field("default", min_length=1, max_length=512)
    speaker: str = Field("default", min_length=1, max_length=512)
    voice: str = Field("en-US-AvaMultilingualNeural", min_length=1, max_length=128)
    speech_speed: float = Field(0.97, ge=0.7, le=1.3)
    volume: float = Field(0.9, ge=0, le=1)
    conversation_mode: str = Field("hands_free", pattern=r"^(push_to_talk|wake_word|hands_free)$")
    always_listening: bool = False
    allow_interruption: bool = True
    emotion_mode: str = Field("automatic", pattern=r"^(automatic|neutral_only)$")
    expressiveness: str = Field("normal", pattern=r"^(low|normal|high)$")


class InterruptionRequest(BaseModel):
    target: InterruptionTarget = InterruptionTarget.SPEECH
