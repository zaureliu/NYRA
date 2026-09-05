from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ListeningMode(StrEnum):
    PUSH_TO_TALK = "push_to_talk"
    WAKE_WORD = "wake_word"
    HANDS_FREE = "hands_free"


class ListeningSettingsUpdate(BaseModel):
    enabled: bool = True
    natural_conversation: bool = True
    mode: ListeningMode = ListeningMode.HANDS_FREE
    wake_word: str = Field("kazumi", min_length=2, max_length=32)
    hands_free_timeout_seconds: int = Field(120, ge=15, le=3600)
    vad_threshold: float = Field(0.5, ge=0, le=1)
    energy_threshold: float = Field(0.018, ge=0.001, le=0.25)
    preroll_ms: int = Field(350, ge=100, le=1000)
    postroll_ms: int = Field(550, ge=200, le=2000)
    speech_start_ms: int = Field(100, ge=40, le=1000)
    max_utterance_seconds: int = Field(60, ge=5, le=300)
    guard_ms: int = Field(400, ge=100, le=3000)
    microphone: str = Field("default", min_length=1, max_length=512)
    barge_in: bool = False
    audio_debug: bool = False
    privacy_indicator: bool = True

    @field_validator("wake_word")
    @classmethod
    def clean_wake_word(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned.replace("-", "").isalpha():
            raise ValueError("Wake word deve conter apenas letras e hífen")
        return cleaned


class ListeningLeaseRequest(BaseModel):
    client_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class PlaybackStateRequest(BaseModel):
    playing: bool
    response_id: str | None = Field(default=None, max_length=100)
    phase: str = Field(default="state", pattern=r"^(state|started|completed|interrupted|failed)$")
    chunk_index: int | None = Field(default=None, ge=0, le=10000)
    spoken_fraction: float | None = Field(default=None, ge=0, le=1)
    barge_in_latency_ms: float | None = Field(default=None, ge=0, le=10000)
    audio_buffer_delay_ms: float | None = Field(default=None, ge=0, le=180000)


class SpeechEndRequest(ListeningLeaseRequest):
    ended_at: float = Field(gt=0)


class WakeWordMatch(BaseModel):
    detected: bool
    wake_word: str
    command_text: str = ""
    confidence: float = 0.0


class UtteranceDecision(BaseModel):
    accepted: bool
    reason: str
    text: str = ""
    wake_word_detected: bool = False
    hands_free_active: bool = False
    close_session: bool = False
