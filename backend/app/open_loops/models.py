"""Schemas for persistent goals, open loops and bounded resume context."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OpenLoopState(StrEnum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


class OpenLoopType(StrEnum):
    GOAL = "GOAL"
    PENDING_INTENTION = "PENDING_INTENTION"
    WAITING_CONDITION = "WAITING_CONDITION"
    BLOCKED_WORK = "BLOCKED_WORK"
    INTERRUPTED_WORK = "INTERRUPTED_WORK"
    GENERAL = "GENERAL"


class GoalState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


class ArtifactReference(BaseModel):
    artifact_id: str | None = Field(default=None, max_length=120)
    path: str = Field(min_length=1, max_length=1200)
    kind: str = Field(default="file", max_length=60)
    exists_state: str = Field(default="unknown", max_length=40)


class ResolutionEvidence(BaseModel):
    kind: str = Field(min_length=2, max_length=80)
    source: str = Field(min_length=2, max_length=120)
    verified: bool = False
    reference_id: str | None = Field(default=None, max_length=160)
    observed_at: datetime = Field(default_factory=utc_now)
    detail: dict[str, Any] = Field(default_factory=dict)


class GoalCreate(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    project: str | None = Field(default=None, max_length=240)
    priority: int = Field(default=50, ge=0, le=100)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = " ".join(value.replace("\x00", "").split())
        if not cleaned:
            raise ValueError("goal title is empty")
        return cleaned


class Goal(GoalCreate):
    id: str = Field(default_factory=lambda: f"goal_{uuid4().hex}")
    state: GoalState = GoalState.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_touched_at: datetime = Field(default_factory=utc_now)


class OpenLoopCreate(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    type: OpenLoopType = OpenLoopType.GENERAL
    state: OpenLoopState = OpenLoopState.OPEN
    goal: str | None = Field(default=None, max_length=240)
    context: dict[str, Any] = Field(default_factory=dict)
    source_turn: str | None = Field(default=None, max_length=160)
    priority: int = Field(default=50, ge=0, le=100)
    related_task: list[str] = Field(default_factory=list, max_length=50)
    related_monitor: list[str] = Field(default_factory=list, max_length=50)
    related_artifact: list[ArtifactReference] = Field(default_factory=list, max_length=50)
    related_project: str | None = Field(default=None, max_length=240)
    waiting_for: dict[str, Any] | None = None
    next_possible_action: str | None = Field(default=None, max_length=1000)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = " ".join(value.replace("\x00", "").split())
        if not cleaned:
            raise ValueError("open loop title is empty")
        return cleaned

    @field_validator("related_task", "related_monitor", mode="before")
    @classmethod
    def listify_relations(cls, value: Any) -> list[str]:
        if value is None:
            return []
        return [str(value)] if isinstance(value, str) else list(value)

    @field_validator("related_artifact", mode="before")
    @classmethod
    def listify_artifacts(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (str, dict, ArtifactReference)):
            return [{"path": value}] if isinstance(value, str) else [value]
        return list(value)


class OpenLoop(OpenLoopCreate):
    id: str = Field(default_factory=lambda: f"loop_{uuid4().hex}")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_touched_at: datetime = Field(default_factory=utc_now)
    dedup_key: str = ""
    resolution_evidence: list[ResolutionEvidence] = Field(default_factory=list)


class OpenLoopTransition(BaseModel):
    state: OpenLoopState
    reason: str = Field(default="operator_update", max_length=500)
    evidence: ResolutionEvidence | None = None


class ResumeContext(BaseModel):
    objective: str
    state: OpenLoopState
    last_confirmed_state: str | None = None
    last_action: str | None = None
    blocker: str | None = None
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    next_possible_action: str | None = None
