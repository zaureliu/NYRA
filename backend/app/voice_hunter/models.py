from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class CandidateStatus(StrEnum):
    SAFE_FOR_NYRA_REFERENCE = "SAFE_FOR_NYRA_REFERENCE"
    SAFE_FOR_DIRECT_TTS = "SAFE_FOR_DIRECT_TTS"
    AUDITION_ONLY = "AUDITION_ONLY"
    REJECTED = "REJECTED"


class HunterPhase(StrEnum):
    IDLE = "IDLE"
    SEARCHING = "SEARCHING"
    CHECKING_LICENSES = "CHECKING_LICENSES"
    DOWNLOADING = "DOWNLOADING"
    ANALYZING = "ANALYZING"
    BENCHMARKING = "BENCHMARKING"
    READY = "READY"
    ERROR = "ERROR"


class CandidateScores(BaseModel):
    brazilian_portuguese: float = Field(ge=0, le=10)
    feminine_identity: float = Field(ge=0, le=10)
    naturalness: float = Field(ge=0, le=10)
    calmness: float = Field(ge=0, le=10)
    conversational_quality: float = Field(ge=0, le=10)
    technical_pronunciation: float = Field(ge=0, le=10)
    audio_cleanliness: float = Field(ge=0, le=10)
    license_suitability: float = Field(ge=0, le=10)
    integration_feasibility: float = Field(ge=0, le=10)

    @property
    def average(self) -> float:
        return round(sum(self.model_dump().values()) / len(type(self).model_fields), 2)


class AudioAnalysis(BaseModel):
    duration_s: float = 0
    sample_rate: int = 0
    channels: int = 0
    rms_dbfs: float | None = None
    peak: float = 0
    clipping_ratio: float = 0
    silence_ratio: float = 0
    approximate_snr_db: float | None = None
    acceptable: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)


class STTValidation(BaseModel):
    language: str = ""
    confidence: float = 0
    transcription: str = ""
    duration_s: float = 0


class LatencyMetrics(BaseModel):
    cold_start_ms: float | None = None
    warm_generation_ms: float | None = None
    total_synthesis_ms: float | None = None
    audio_duration_ms: float | None = None
    real_time_factor: float | None = None
    time_to_first_audio_ms: float | None = None
    measured_at: str | None = None


class VoiceCandidate(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    name: str
    source: str
    source_url: HttpUrl
    type: Literal["Synthetic", "Dataset", "TTS Provider", "Model"]
    language: str = "pt-BR"
    gender: str = "Female"
    license: str
    license_url: HttpUrl | None = None
    allowed_use: str
    reference_allowed: bool = False
    redistributable: bool = False
    commercial_use: bool | None = None
    identity_terms: str = ""
    size_bytes: int | None = Field(default=None, ge=0)
    provider: str
    provider_voice: str | None = None
    location: Literal["LOCAL", "ONLINE", "PAID_OPTION"]
    naturalness_estimate: float = Field(ge=0, le=10)
    integration_difficulty: Literal["LOW", "MEDIUM", "HIGH"]
    status: CandidateStatus
    scores: CandidateScores
    technical_score: float = Field(default=0, ge=0, le=10)
    top_candidate: bool = False
    sample_file: str | None = None
    sample_url: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    analysis: AudioAnalysis | None = None
    stt: STTValidation | None = None
    latency: LatencyMetrics | None = None
    benchmark: dict[str, Any] = Field(default_factory=dict)
    favorite: bool = False
    discarded: bool = False
    my_rating: float | None = Field(default=None, ge=0, le=10)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_license_safety(self) -> "VoiceCandidate":
        if self.status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE and not self.reference_allowed:
            raise ValueError("SAFE_FOR_NYRA_REFERENCE exige reference_allowed=true")
        if self.status == CandidateStatus.REJECTED and self.top_candidate:
            raise ValueError("candidata rejeitada não pode ser top candidate")
        return self

    def refresh_score(self) -> None:
        self.technical_score = self.scores.average


class VoiceHunterState(BaseModel):
    phase: HunterPhase = HunterPhase.IDLE
    progress: int = Field(default=0, ge=0, le=100)
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    candidate_count: int = 0
    downloaded_bytes: int = 0
    download_budget_bytes: int = 8 * 1024**3
    cancelled: bool = False
