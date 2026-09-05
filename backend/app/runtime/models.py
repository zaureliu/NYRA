"""KAZUMI Runtime Supervisor V1 - normalized models for persistent service management."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RuntimeState(StrEnum):
    UNKNOWN = "UNKNOWN"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    RESTARTING = "RESTARTING"
    FAILED = "FAILED"
    CRASH_LOOP = "CRASH_LOOP"
    DISABLED = "DISABLED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"


class Ownership(StrEnum):
    OWNED = "OWNED"
    EXTERNAL = "EXTERNAL"


class RuntimeType(StrEnum):
    PROCESS = "PROCESS"
    WINDOWS_SERVICE = "WINDOWS_SERVICE"
    DOCKER_CONTAINER = "DOCKER_CONTAINER"
    DOCKER_COMPOSE = "DOCKER_COMPOSE"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"


class HealthKind(StrEnum):
    NONE = "NONE"
    PROCESS = "PROCESS"
    TCP = "TCP"
    HTTP = "HTTP"
    COMMAND = "COMMAND"
    WARM_MANAGER = "WARM_MANAGER"
    SENTINEL = "SENTINEL"


class StartupPolicy(StrEnum):
    @classmethod
    def _missing_(cls, value):
        if value == "ON_NYRA_START":
            return cls.ON_KAZUMI_START
        return None

    MANUAL = "MANUAL"
    ON_KAZUMI_START = "ON_KAZUMI_START"
    MONITOR_ONLY = "MONITOR_ONLY"


class ShutdownPolicy(StrEnum):
    LEAVE_RUNNING = "LEAVE_RUNNING"
    TERMINATE = "TERMINATE"


class Capabilities(BaseModel):
    status: bool = True
    health: bool = True
    start: bool = False
    stop: bool = False
    restart: bool = False
    logs: bool = False


class AutoRecovery(BaseModel):
    enabled: bool = False
    max_attempts: int = Field(2, ge=1, le=10)
    cooldown_seconds: int = Field(60, ge=5, le=3600)


class RiskPolicy(BaseModel):
    """Risk classification per action, integrated with the existing classifier levels."""

    status: str = "READ_ONLY"
    health: str = "READ_ONLY"
    logs: str = "READ_ONLY"
    start: str = "LOW_RISK"
    stop: str = "ELEVATED"
    restart: str = "ELEVATED"


class HealthSpec(BaseModel):
    kind: HealthKind
    url: str | None = None
    host: str = "127.0.0.1"
    port: int | None = Field(None, ge=1, le=65535)
    command: list[str] | None = None
    timeout_seconds: float = Field(3.0, gt=0, le=30)
    expected_status: int = Field(200, ge=100, le=399)
    process_match: list[str] = Field(default_factory=list)


class ReadinessKind(StrEnum):
    NONE = "NONE"
    HEALTH_PASS = "HEALTH_PASS"
    OLLAMA_WARM = "OLLAMA_WARM"


class ReadinessSpec(BaseModel):
    kind: ReadinessKind = ReadinessKind.HEALTH_PASS


class ServiceSpec(BaseModel):
    """One registered persistent service. Commands come ONLY from this trusted registry."""

    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    type: RuntimeType
    ownership: Ownership = Ownership.EXTERNAL
    platform: str = "windows"
    working_directory: str | None = None
    start_command: list[str] | None = None
    stop_grace_seconds: float = Field(5.0, gt=0, le=60)
    shutdown_policy: ShutdownPolicy = ShutdownPolicy.LEAVE_RUNNING
    health: HealthSpec | None = None
    readiness: ReadinessSpec = Field(default_factory=ReadinessSpec)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    startup_policy: StartupPolicy = StartupPolicy.MANUAL
    auto_recovery: AutoRecovery = Field(default_factory=AutoRecovery)
    risk: RiskPolicy = Field(default_factory=RiskPolicy)
    depends_on: list[str] = Field(default_factory=list)
    log_path: str | None = None
    log_max_bytes: int = Field(1_000_000, ge=10_000, le=50_000_000)
    log_backup_count: int = Field(2, ge=1, le=10)
    startup_timeout_seconds: float = Field(30.0, ge=1, le=300)
    health_interval_seconds: float = Field(15.0, ge=2, le=600)
    notes: str = ""

    def resolved_log_path(self, logs_root) -> PathLike:  # type: ignore[no-untyped-def]
        from pathlib import Path

        if self.log_path:
            configured = Path(self.log_path)
            return configured if configured.is_absolute() else Path(logs_root) / configured
        return Path(logs_root) / "runtime" / f"{self.id}.log"


PathLike = object


class IdentityRecord(BaseModel):
    pid: int | None = None
    create_time: float | None = None
    exe_path: str | None = None
    cmdline_fingerprint: str | None = None

    def matches(self, other: "IdentityRecord", require_all: bool = True) -> bool:
        fields = ("pid", "create_time", "exe_path", "cmdline_fingerprint")
        checks = [getattr(self, f) == getattr(other, f) for f in fields if getattr(self, f) is not None]
        if not checks:
            return False
        return all(checks) if require_all else any(checks)


class HealthResult(BaseModel):
    healthy: bool
    latency_ms: float = 0
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceSnapshot(BaseModel):
    id: str
    display_name: str = ""
    state: RuntimeState = RuntimeState.UNKNOWN
    ownership: Ownership = Ownership.EXTERNAL
    type: RuntimeType = RuntimeType.EXTERNAL_SERVICE
    pid: int | None = None
    uptime_seconds: float | None = None
    restart_count: int = 0
    last_error: str | None = None
    health: dict[str, Any] | None = None
    readiness: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    startup_policy: StartupPolicy = StartupPolicy.MANUAL
    validation_error: str | None = None


class OperationErrorCodes(StrEnum):
    UNKNOWN_SERVICE = "UNKNOWN_SERVICE"
    SERVICE_DISABLED = "SERVICE_DISABLED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    ALREADY_STOPPED = "ALREADY_STOPPED"
    CRASH_LOOP_PROTECTED = "CRASH_LOOP_PROTECTED"
    OPERATION_LOCKED = "OPERATION_LOCKED"
    SELF_RESTART_UNSUPPORTED = "SELF_RESTART_UNSUPPORTED"
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
    SPAWN_FAILED = "SPAWN_FAILED"
    STOP_FAILED = "STOP_FAILED"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    UNSUPPORTED_RUNTIME = "UNSUPPORTED_RUNTIME"
    RUNTIME_SUPERVISOR_DISABLED = "RUNTIME_SUPERVISOR_DISABLED"


def operation_result(
    *,
    success: bool,
    service: str,
    action: str,
    state: RuntimeState,
    error_code: str | None = None,
    message: str = "",
    detail: dict[str, Any] | None = None,
    duration_ms: float = 0,
    execution_success: bool | None = None,
    effect_verified: bool | None = None,
    verification_status: str = "NOT_REQUIRED",
    approval_required: bool = False,
    approval_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": success,
        "service": service,
        "action": action,
        "state": state.value,
        "error_code": error_code,
        "message": message,
        "duration_ms": round(duration_ms, 1),
        "approval_required": approval_required,
        "execution_success": execution_success,
        "effect_verified": effect_verified,
        "verification_status": verification_status,
    }
    if approval_id:
        payload["approval_id"] = approval_id
    if detail:
        payload.update(detail)
    return payload
