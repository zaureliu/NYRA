"""Schemas for controlled, event-driven proactive presence."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProactiveDecision(StrEnum):
    IGNORE = "IGNORE"
    LOG_ONLY = "LOG_ONLY"
    UI_NOTIFICATION = "UI_NOTIFICATION"
    CHAT_MESSAGE = "CHAT_MESSAGE"
    VOICE_AND_CHAT = "VOICE_AND_CHAT"
    DEFER = "DEFER"


class ProactivePriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"LOW": 1, "NORMAL": 2, "HIGH": 3, "CRITICAL": 4}[self.value]


class ProactiveMode(StrEnum):
    NORMAL = "NORMAL"
    QUIET = "QUIET"
    DO_NOT_DISTURB = "DO_NOT_DISTURB"


class ProactiveSettings(BaseModel):
    enabled: bool = True
    mode: ProactiveMode = ProactiveMode.NORMAL
    voice_enabled: bool = False
    default_cooldown_seconds: int = Field(default=300, ge=10, le=86400)
    max_notifications_per_hour: int = Field(default=6, ge=1, le=60)
    defer_ttl_seconds: int = Field(default=1800, ge=30, le=86400)


class ProactiveSettingsUpdate(BaseModel):
    enabled: bool | None = None
    mode: ProactiveMode | None = None
    voice_enabled: bool | None = None


class ProactiveCandidate(BaseModel):
    event_id: str = Field(max_length=80)
    event_type: str = Field(max_length=120)
    source: str = Field(max_length=80)
    entity: str = Field(default="system", max_length=240)
    goal_id: str | None = Field(default=None, max_length=160)
    open_loop_id: str | None = Field(default=None, max_length=160)
    message: str = Field(max_length=500)
    priority: ProactivePriority = ProactivePriority.NORMAL
    impact: float = Field(default=.5, ge=0, le=1)
    urgency: float = Field(default=.5, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    semantic_family: str = Field(max_length=120)
    baseline: Literal["IGNORE", "LOG_ONLY", "EVALUATE"] = "EVALUATE"
    recovery_of: str | None = Field(default=None, max_length=300)
    opens_incident: str | None = Field(default=None, max_length=300)
    voice_requested: bool = False
    occurred_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        goal = self.goal_id or "none"
        return (
            f"{self.semantic_family}|{self.source}|{self.entity.casefold()}|"
            f"{goal}|{self.priority.value}"
        )


class DecisionContext(BaseModel):
    user_activity: str = "UNKNOWN"
    assistant_state: str = "IDLE"
    relation_to_active_goal: float = Field(default=0, ge=0, le=1)
    relation_to_recent_request: float = Field(default=0, ge=0, le=1)
    novelty: float = Field(default=1, ge=0, le=1)
    repeat_count: int = Field(default=0, ge=0)
    freshness: float = Field(default=1, ge=0, le=1)
    cooldown_active: bool = False
    recovery_relevant: bool = True
    voice_ready: bool = False
    notifications_last_hour: int = Field(default=0, ge=0)


class DecisionRecord(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"pdec_{uuid4().hex}")
    event_id: str
    event_type: str
    source: str
    entity: str
    goal_id: str | None = None
    open_loop_id: str | None = None
    priority: ProactivePriority
    score: float = Field(ge=0, le=100)
    decision: ProactiveDecision
    reason: str = Field(max_length=600)
    repeat_count: int = Field(default=0, ge=0)
    dedup_key: str = Field(max_length=700)
    created_at: datetime = Field(default_factory=utc_now)
    execution_authorized: bool = False
    action_budget_consumed: int = 0


class ProactiveNotification(BaseModel):
    notification_id: str = Field(default_factory=lambda: f"pnot_{uuid4().hex}")
    decision_id: str
    event_id: str
    event_type: str
    source: str
    entity: str
    goal_id: str | None = None
    open_loop_id: str | None = None
    message: str = Field(max_length=500)
    priority: ProactivePriority
    channels: list[Literal["ui", "chat", "voice"]]
    read: bool = False
    repeat_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    execution_authorized: bool = False
    action_budget_consumed: int = 0
    dialogue_policy: str | None = Field(default=None, max_length=80)
    emotion: dict[str, Any] | None = None
