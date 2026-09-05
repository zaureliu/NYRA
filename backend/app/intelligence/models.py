from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryKind(StrEnum):
    WORKING = "working"
    CONVERSATION = "conversation"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROJECT = "project"
    OPERATIONAL = "operational"
    USER_PREFERENCE = "user_preference"
    TOOL_HISTORY = "tool_history"


class Sensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"
    SECRET = "SECRET"


class TrustBoundary(StrEnum):
    SYSTEM_TRUSTED = "SYSTEM_TRUSTED"
    USER_INPUT = "USER_INPUT"
    TOOL_TRUSTED = "TOOL_TRUSTED"
    TOOL_UNTRUSTED = "TOOL_UNTRUSTED"
    REMOTE_CONTENT = "REMOTE_CONTENT"
    WEB_CONTENT = "WEB_CONTENT"
    DOCUMENT_CONTENT = "DOCUMENT_CONTENT"
    MEMORY_CONTENT = "MEMORY_CONTENT"


class RuntimeState(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"
    UNCONFIGURED = "UNCONFIGURED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class EvidenceLevel(StrEnum):
    OBSERVED = "OBSERVED"
    CORRELATED = "CORRELATED"
    INFERRED = "INFERRED"
    CONFIRMED = "CONFIRMED"


class AutonomousTaskState(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class TraceStage(StrEnum):
    USER_REQUEST = "USER_REQUEST"
    CONTEXT_ASSEMBLY = "CONTEXT_ASSEMBLY"
    MODEL_ROUTE = "MODEL_ROUTE"
    PLAN = "PLAN"
    POLICY_DECISIONS = "POLICY_DECISIONS"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    VERIFICATION = "VERIFICATION"
    RETRY = "RETRY"
    FINAL_DECISION = "FINAL_DECISION"
    RESPONSE = "RESPONSE"


class MemoryWrite(BaseModel):
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=20_000)
    source: str = Field(default="user", max_length=120)
    category: str = Field(default="general", max_length=120)
    project: str | None = Field(default=None, max_length=240)
    confidence: float = Field(default=0.7, ge=0, le=1)
    relevance: float = Field(default=0.7, ge=0, le=1)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    expires_at: datetime | None = None
    decay_half_life_days: float | None = Field(default=90, ge=0.01, le=3650)
    provenance: dict[str, Any] = Field(default_factory=dict)
    related_entities: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        cleaned = value.replace("\x00", "").strip()
        if not cleaned:
            raise ValueError("memory content is empty")
        return cleaned


class MemoryItem(MemoryWrite):
    id: str
    created_at: datetime
    updated_at: datetime
    score: float = 0
    conflict: bool = False
    conflict_group: str | None = None


class KnowledgeHit(BaseModel):
    document_id: str
    chunk_id: str
    path: str
    content: str
    mime_type: str
    score: float
    chunk_index: int
    provenance: dict[str, Any]
    trust: TrustBoundary = TrustBoundary.DOCUMENT_CONTENT


class ContextBlock(BaseModel):
    source: str
    content: str
    trust: TrustBoundary
    priority: int = Field(ge=0, le=100)
    relevance: float = Field(ge=0, le=1)
    characters: int = Field(ge=0)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ContextAssembly(BaseModel):
    assembly_id: str = Field(default_factory=lambda: uuid4().hex)
    blocks: list[ContextBlock]
    used_characters: int
    budget_characters: int
    dropped_blocks: int
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ModelRoute(BaseModel):
    route_id: str = Field(default_factory=lambda: uuid4().hex)
    task_type: str
    selected_model: str | None
    fallback_models: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    context_characters: int = 0
    reason: str
    inventory_state: RuntimeState
    resource_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AutonomousTaskSpec(BaseModel):
    task_id: str = Field(default_factory=lambda: f"task_{uuid4().hex}")
    title: str = Field(min_length=3, max_length=180)
    objective: str = Field(min_length=3, max_length=4000)
    goal_id: str | None = Field(default=None, max_length=160)
    source_turn: str | None = Field(default=None, max_length=160)
    trigger: str = Field(default="one_shot", pattern=r"^(one_shot|schedule|recurring|event|conditional)$")
    schedule: str | None = Field(default=None, max_length=200)
    conditions: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list, max_length=30)
    policy: dict[str, Any] = Field(default_factory=dict)
    risk: str = Field(default="READ_ONLY", pattern=r"^(READ_ONLY|LOW_RISK|ELEVATED|DESTRUCTIVE|CRITICAL)$")
    approval_mode: str = Field(default="policy", pattern=r"^(policy|always|never)$")
    action: str = Field(min_length=2, max_length=120)
    parameters: dict[str, Any] = Field(default_factory=dict)
    state: AutonomousTaskState = AutonomousTaskState.CREATED
    retries: int = Field(default=0, ge=0, le=20)
    max_retries: int = Field(default=2, ge=0, le=20)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    last_run: datetime | None = None
    next_run: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class IntelligenceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    timestamp: datetime = Field(default_factory=utc_now)
    source: str = Field(max_length=120)
    category: str = Field(max_length=120)
    severity: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    entity: str | None = Field(default=None, max_length=240)
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=120)
    evidence_level: EvidenceLevel = EvidenceLevel.OBSERVED
    confidence: float = Field(default=1, ge=0, le=1)


class DiagnosisResult(BaseModel):
    diagnosis: str
    probable_cause: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    failed_checks: list[dict[str, Any]] = Field(default_factory=list)
    passed_checks: list[dict[str, Any]] = Field(default_factory=list)
    recommended_action: str | None = None
    optional_automated_action: dict[str, Any] | None = None


class TraceEntry(BaseModel):
    trace_id: str
    sequence: int = Field(ge=1)
    stage: TraceStage
    timestamp: datetime = Field(default_factory=utc_now)
    component: str
    operation: str
    correlation_id: str | None = None
    task_id: str | None = None
    severity: str = "INFO"
    duration_ms: float | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
