from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class STTState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    CONNECTING = "CONNECTING"
    STREAMING = "STREAMING"
    DEGRADED = "DEGRADED"
    FALLBACK = "FALLBACK"
    AUTH_ERROR = "AUTH_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    ERROR = "ERROR"


class STTSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["deepgram", "faster_whisper"] = "faster_whisper"
    model: Literal["nova-3"] = "nova-3"
    language: str = Field("pt-BR", pattern=r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$", max_length=12)
    smart_format: bool = True
    interim_results: bool = True
    utterance_end_ms: int = Field(1000, ge=1000, le=5000)
    endpointing: int = Field(300, ge=100, le=2000)
    vad_events: bool = True
    punctuate: bool = True
    numerals: bool = True
    profanity_filter: bool = False
    diarize: bool = False
    redact: Literal[False] = False
    dictation: bool = False
    fallback: Literal["faster_whisper"] = "faster_whisper"
    keyterms_enabled: bool = False
    keyterms: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("keyterms")
    @classmethod
    def validate_keyterms(cls, terms: list[str]) -> list[str]:
        terms = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
        if any(len(term) > 80 or any(ord(c) < 32 for c in term) for term in terms):
            raise ValueError("Keyterms must be short plain terms")
        return terms


class AudioFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    encoding: Literal["linear16"] = "linear16"
    sample_rate: int = Field(48000, ge=8000, le=96000)
    channels: Literal[1] = 1

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * 2


class STTCapabilities(BaseModel):
    streaming: bool = False
    buffered_audio: bool = True
    interim_results: bool = False
    speech_started: bool = False
    endpointing: bool = False
    utterance_end: bool = False
    remote: bool = False
    word_timestamps: bool = False


class TranscriptWord(BaseModel):
    text: str
    started_at: float
    ended_at: float
    confidence: float | None = None


class CanonicalTranscript(BaseModel):
    text: str
    is_final: bool
    speech_final: bool = False
    confidence: float | None = None
    started_at: float = 0
    ended_at: float = 0
    provider: str
    language: str
    words: list[TranscriptWord] = Field(default_factory=list)
    utterance_id: str
    sequence: int


class RecognitionEvent(BaseModel):
    type: Literal["interim", "final", "speech_started", "speech_final", "utterance_end", "state"]
    transcript: CanonicalTranscript | None = None
    timestamp: float | None = None
    state: STTState | None = None


EventSink = Callable[[RecognitionEvent], Awaitable[None]]


class STTFailure(Exception):
    """Only fixed public codes cross this boundary, never provider exceptions."""

    def __init__(self, state: STTState, code: str):
        self.state = state
        self.code = code
        super().__init__(code)


class RealtimeSTTProvider(ABC):
    """Incremental audio input. Capabilities distinguish online vs buffered STT.

    The event sink receives canonical interim/final and provider VAD events.
    finish() returns one assembled utterance; only that result can become a turn.
    """

    state: STTState = STTState.READY

    @abstractmethod
    def capabilities(self) -> STTCapabilities: ...

    @abstractmethod
    async def connect(self) -> None: ...

    async def start_stream(self) -> None:
        await self.connect()

    @abstractmethod
    async def send_audio(self, audio: bytes) -> None: ...

    @abstractmethod
    async def finish(self) -> CanonicalTranscript: ...

    @abstractmethod
    async def close(self) -> None: ...

    def health(self) -> STTState:
        return self.state
