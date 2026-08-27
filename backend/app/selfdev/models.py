from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SelfDevMode(StrEnum):
    OFF = "OFF"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    AUTONOMOUS_SAFE = "AUTONOMOUS_SAFE"
    AUTONOMOUS_ADVANCED = "AUTONOMOUS_ADVANCED"


class IssueType(StrEnum):
    BUG = "BUG"
    PERFORMANCE = "PERFORMANCE"
    RELIABILITY = "RELIABILITY"
    INTEGRATION = "INTEGRATION"
    CONCURRENCY = "CONCURRENCY"
    RESOURCE_LEAK = "RESOURCE_LEAK"
    DEAD_CODE = "DEAD_CODE"
    DUPLICATION = "DUPLICATION"
    FUNCTIONAL_UI = "FUNCTIONAL_UI"
    TEST_GAP = "TEST_GAP"
    SECURITY_HARDENING = "SECURITY_HARDENING"
    FEATURE_GAP_EXPLICIT = "FEATURE_GAP_EXPLICIT"


class IssueStatus(StrEnum):
    DETECTED = "DETECTED"
    EVIDENCE_GATHERING = "EVIDENCE_GATHERING"
    READY_FOR_PLANNING = "READY_FOR_PLANNING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    READY_TO_PROMOTE = "READY_TO_PROMOTE"
    PROMOTING = "PROMOTING"
    POST_VALIDATING = "POST_VALIDATING"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class SelfDevRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TaskComplexity(StrEnum):
    TRIVIAL = "TRIVIAL"
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"
    ARCHITECTURAL = "ARCHITECTURAL"


class NotificationType(StrEnum):
    IMPROVEMENT_APPLIED = "IMPROVEMENT_APPLIED"
    IMPROVEMENT_PREPARED = "IMPROVEMENT_PREPARED"
    ROLLBACK_OCCURRED = "ROLLBACK_OCCURRED"
    ISSUE_DETECTED = "ISSUE_DETECTED"
    BLOCKED = "BLOCKED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"


class Evidence(BaseModel):
    source: str = Field(max_length=120)
    metric: str = Field(max_length=120)
    value: float | int | str | bool | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    context: dict[str, str | float | int | bool | None] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def bound_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {str(key)[:80]: item for key, item in list(value.items())[:20]}


class ImprovementIssue(BaseModel):
    issue_id: str = Field(default_factory=lambda: f"SELFDEV-{uuid4().hex[:8].upper()}")
    type: IssueType
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=3, max_length=2000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    source: str = Field(default="runtime", max_length=120)
    first_seen: datetime = Field(default_factory=utc_now)
    last_seen: datetime = Field(default_factory=utc_now)
    occurrences: int = Field(default=1, ge=1)
    affected_components: list[str] = Field(default_factory=list, max_length=30)
    estimated_benefit: str = Field(default="", max_length=500)
    risk: SelfDevRisk = SelfDevRisk.MEDIUM
    priority: int = Field(default=50, ge=0, le=100)
    status: IssueStatus = IssueStatus.DETECTED
    fingerprint: str = Field(default="", max_length=128)
    attempts: int = Field(default=0, ge=0)
    failure_reasons: list[str] = Field(default_factory=list, max_length=20)
    cooldown_until: datetime | None = None
    last_candidate: str | None = None
    last_validation: str | None = None


class SelfDevPlan(BaseModel):
    issue_id: str
    root_cause_hypothesis: str = Field(min_length=10, max_length=2000)
    evidence: list[Evidence] = Field(min_length=1, max_length=100)
    files_expected: list[str] = Field(default_factory=list, max_length=50)
    symbols_expected: list[str] = Field(default_factory=list, max_length=100)
    test_plan: list[str] = Field(default_factory=list, max_length=50)
    benchmark_plan: list[str] = Field(default_factory=list, max_length=30)
    rollback_plan: list[str] = Field(default_factory=list, max_length=30)
    risk: SelfDevRisk
    complexity: TaskComplexity
    acceptance_criteria: list[str] = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=utc_now)


class FileChange(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    operation: Literal["CREATE", "UPDATE"]
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")

    @field_validator("path")
    @classmethod
    def relative_safe_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError("change path must be relative and contained")
        return path.as_posix()


class PatchBundle(BaseModel):
    issue_id: str
    changes: list[FileChange] = Field(min_length=1, max_length=50)
    rationale: str = Field(min_length=3, max_length=2000)


class ValidationStep(BaseModel):
    name: str
    command: str = ""
    status: Literal["PASS", "FAIL", "SKIPPED", "BLOCKED"]
    elapsed_seconds: float = Field(default=0, ge=0)
    output_summary: str = Field(default="", max_length=4000)


class ValidationReport(BaseModel):
    issue_id: str
    candidate_path: str
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    passed: bool = False
    steps: list[ValidationStep] = Field(default_factory=list)
    security_findings: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)


class BenchmarkMeasurement(BaseModel):
    metric: str
    before: float
    after: float
    delta_percent: float
    sample_count: int = Field(ge=1)
    improved: bool


class PromotionRecord(BaseModel):
    issue_id: str
    candidate_commit: str
    promotion_commit: str | None = None
    applied_at: datetime = Field(default_factory=utc_now)
    post_validation: Literal["PENDING", "PASS", "FAIL"] = "PENDING"
    rollback_status: Literal["NOT_REQUIRED", "PENDING", "APPLIED", "FAILED"] = "NOT_REQUIRED"
    github_status: Literal["OFF", "PENDING", "PUBLISHED", "BLOCKED", "FAILED"] = "OFF"


class SelfDevNotification(BaseModel):
    notification_id: str = Field(default_factory=lambda: uuid4().hex)
    type: NotificationType
    issue_id: str | None = None
    title: str = Field(max_length=180)
    message: str = Field(max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)
    read: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class SelfDevSettings(BaseModel):
    mode: SelfDevMode = SelfDevMode.AUTONOMOUS_SAFE
    model: str = Field(default="qwen3:8b", min_length=2, max_length=120)
    workspace: Path
    canonical_root: Path
    public_snapshot: Path
    run_when_idle: bool = True
    auto_publish_github: bool = False
    max_auto_promotions_per_day: int = Field(default=3, ge=0, le=20)
    max_concurrent_tasks: int = Field(default=1, ge=1, le=4)
    max_candidate_runtime_minutes: int = Field(default=30, ge=1, le=240)
    max_files_low_risk: int = Field(default=8, ge=1, le=100)
    max_diff_lines_low_risk: int = Field(default=500, ge=1, le=20_000)
    cooldown_minutes: int = Field(default=15, ge=0, le=1440)
    max_cpu_percent: float = Field(default=65, ge=5, le=100)
    min_free_ram_gb: float = Field(default=2, ge=0.25, le=128)
    keep_last_completed_worktrees: int = Field(default=10, ge=1, le=100)
    pause_when_voice_active: bool = True
    pause_when_user_active: bool = True


class SelfDevStatus(BaseModel):
    state: Literal["OFF", "STARTING", "READY", "DEGRADED", "BUSY", "BLOCKED"]
    mode: SelfDevMode
    active_issue_id: str | None = None
    queue_size: int = 0
    unread_notifications: int = 0
    repository_files: int = 0
    workspace_ready: bool = False
    github_status: str = "OFF"
    last_error_code: str | None = None
