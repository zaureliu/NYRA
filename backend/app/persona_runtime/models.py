"""Typed public contracts for NYRA's persistent persona runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BehaviourLevel(StrEnum):
    LOW = "low"
    LOW_MEDIUM = "low-medium"
    MEDIUM = "medium"
    MEDIUM_HIGH = "medium-high"
    HIGH = "high"


class NyraEmotion(StrEnum):
    NEUTRAL = "neutral"
    FRIENDLY = "friendly"
    FOCUSED = "focused"
    CONFIDENT = "confident"
    POSITIVE = "positive"
    HAPPY = "happy"
    RELIEVED = "relieved"
    CONCERNED = "concerned"
    WARNING = "warning"
    SERIOUS = "serious"
    EMPATHETIC = "empathetic"
    CURIOUS = "curious"
    SURPRISED = "surprised"
    AMUSED = "amused"
    APOLOGETIC = "apologetic"
    UNCERTAIN = "uncertain"
    CALM = "calm"


class DialogueMode(StrEnum):
    CASUAL_CHAT = "casual_chat"
    INFORM = "inform"
    EXPLAIN = "explain"
    TECHNICAL_DIAGNOSIS = "technical_diagnosis"
    ASK = "ask"
    CONFIRM = "confirm"
    WARN = "warn"
    CHALLENGE = "challenge"
    JOKE = "joke"
    PLAYFUL_REPLY = "playful_reply"
    EXECUTE = "execute"
    REPORT_RESULT = "report_result"
    APOLOGIZE = "apologize"
    META_IDENTITY = "meta_identity"
    META_VOICE = "meta_voice"


class PersonalityProfile(BaseModel):
    """Stable behavioural parameters, not a psychological assessment."""

    model_config = ConfigDict(frozen=True)

    directness: BehaviourLevel = BehaviourLevel.HIGH
    curiosity: BehaviourLevel = BehaviourLevel.HIGH
    confidence: BehaviourLevel = BehaviourLevel.MEDIUM_HIGH
    playfulness: BehaviourLevel = BehaviourLevel.MEDIUM
    formality: BehaviourLevel = BehaviourLevel.LOW_MEDIUM
    technical_orientation: BehaviourLevel = BehaviourLevel.HIGH
    initiative: BehaviourLevel = BehaviourLevel.MEDIUM_HIGH
    caution: BehaviourLevel = BehaviourLevel.MEDIUM_HIGH


class NyraIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity_id: str = "nyra-core-v1"
    name: str = "NYRA"
    nature: str = "local artificial intelligence"
    language: str = "pt-BR"
    identity_version: int = 1
    personality: PersonalityProfile = Field(default_factory=PersonalityProfile)
    invariants: tuple[str, ...] = (
        "knows_it_is_ai",
        "truth_before_performance",
        "local_first",
        "no_llm_text_execution_authority",
        "core_identity_rejects_prompt_drift",
    )


class RelationshipState(BaseModel):
    """Useful communication adaptation; deliberately excludes attachment scores."""

    familiarity: float = Field(default=0.0, ge=0.0, le=1.0)
    interaction_style: str = Field(default="direct_collaborative", max_length=80)
    preferred_technical_depth: str = Field(default="adaptive", pattern=r"^(concise|adaptive|deep)$")
    humor_tolerance: str = Field(default="moderate", pattern=r"^(low|moderate|high)$")
    communication_preferences: list[str] = Field(default_factory=list, max_length=20)
    interaction_count: int = Field(default=0, ge=0)
    learning_evidence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("communication_preferences")
    @classmethod
    def compact_preferences(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            cleaned = " ".join(str(value).split())[:160]
            if cleaned and cleaned.casefold() not in {item.casefold() for item in result}:
                result.append(cleaned)
        return result[:20]


class EmotionDecayPolicy(BaseModel):
    policy_id: str = "contextual-v1"
    half_life_seconds: float = Field(default=900.0, ge=1.0, le=86400.0)
    neutral_threshold: float = Field(default=0.12, ge=0.0, le=0.5)
    max_restore_age_seconds: float = Field(default=21600.0, ge=60.0, le=259200.0)


class EmotionalState(BaseModel):
    primary: NyraEmotion = NyraEmotion.NEUTRAL
    intensity: float = Field(default=0.0, ge=0.0, le=0.65)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str = Field(default="baseline", max_length=180)
    started_at: datetime = Field(default_factory=utc_now)
    last_updated: datetime = Field(default_factory=utc_now)
    decay_policy: EmotionDecayPolicy = Field(default_factory=EmotionDecayPolicy)


class DialoguePolicy(BaseModel):
    mode: DialogueMode = DialogueMode.INFORM
    directness: str = Field(default="direct", pattern=r"^(direct|balanced|gentle)$")
    technical_depth: str = Field(default="adaptive", pattern=r"^(concise|adaptive|deep)$")
    humor_allowed: bool = False
    requires_grounding: bool = False
    reason: str = Field(default="default", max_length=180)
    updated_at: datetime = Field(default_factory=utc_now)


class VoiceEmotionInterface(BaseModel):
    emotion: NyraEmotion
    intensity: float = Field(ge=0.0, le=0.65)
    style: str
    provider_supports_emotion: bool
    acoustic_emotion: str
    degraded: bool
    degradation_reason: str | None = None


class PersonaSnapshot(BaseModel):
    identity: NyraIdentity
    relationship: RelationshipState
    emotion: EmotionalState
    dialogue_policy: DialoguePolicy
    temporary_style: str | None = None
    generated_at: datetime = Field(default_factory=utc_now)


class RelationshipEvidence(BaseModel):
    key: str = Field(pattern=r"^(preferred_technical_depth|humor_tolerance|interaction_style|communication_preference)$")
    value: str = Field(min_length=1, max_length=160)
    explicit: bool = False


class DriftDecision(BaseModel):
    permanent_change_applied: bool = False
    drift_blocked: bool = False
    temporary_style: str | None = None
    reason: str
