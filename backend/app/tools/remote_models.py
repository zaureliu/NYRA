from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import ipaddress

from pydantic import BaseModel, Field, field_validator

from app.tools.grounding import VerificationStatus
from app.tools.shell_models import ShellRiskLevel


class RemoteShellErrorCode(StrEnum):
    REMOTE_SHELL_DISABLED = "REMOTE_SHELL_DISABLED"
    UNKNOWN_TRUSTED_HOST = "UNKNOWN_TRUSTED_HOST"
    HOST_DISABLED = "HOST_DISABLED"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    SSH_CLIENT_UNAVAILABLE = "SSH_CLIENT_UNAVAILABLE"
    SSH_CREDENTIALS_MISSING = "SSH_CREDENTIALS_MISSING"
    SSH_KNOWN_HOSTS_MISSING = "SSH_KNOWN_HOSTS_MISSING"
    SSH_HOST_KEY_MISMATCH = "SSH_HOST_KEY_MISMATCH"
    SSH_AUTHENTICATION_FAILED = "SSH_AUTHENTICATION_FAILED"
    SSH_CONNECTION_TIMEOUT = "SSH_CONNECTION_TIMEOUT"
    SSH_CONNECTION_FAILED = "SSH_CONNECTION_FAILED"
    SSH_COMMAND_TIMEOUT = "SSH_COMMAND_TIMEOUT"
    SSH_COMMAND_FAILED = "SSH_COMMAND_FAILED"
    INVALID_COMMAND = "INVALID_COMMAND"
    INVALID_WORKING_DIRECTORY = "INVALID_WORKING_DIRECTORY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    AGENT_READ_ONLY = "AGENT_READ_ONLY"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"


class RemoteShellExecuteInput(BaseModel):
    host: str = Field(
        min_length=1,
        max_length=100,
        description="ID ou alias lógico de um host previamente cadastrado. Nunca informe IP, usuário ou porta.",
    )
    command: str = Field(max_length=32_768, description="Comando remoto completo e finito.")
    timeout_seconds: int | None = Field(default=None, ge=1, le=3_600)
    working_directory: str | None = Field(default=None, max_length=2_048)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=500)

    @field_validator("host")
    @classmethod
    def logical_host_only(cls, value: str) -> str:
        clean = " ".join(value.strip().split())
        try:
            ipaddress.ip_address(clean.strip("[]"))
        except ValueError:
            return clean
        raise ValueError("Use apenas um ID ou alias do Trusted Host Registry; IPs diretos são proibidos")


class RemotePolicyAssessment(BaseModel):
    risk_level: ShellRiskLevel
    reasons: list[str] = Field(default_factory=list)
    required_capability: str = "diagnostics"
    normalized_action: str | None = None
    resource_type: str | None = None
    resource_name: str | None = None
    auto_remediation_allowed: bool = False


class RemoteExecutionResult(BaseModel):
    success: bool
    execution_id: str | None = None
    agent_run_id: str | None = None
    host: str
    address: str
    port: int
    platform: str
    command: str
    working_directory: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0
    timed_out: bool = False
    risk_level: ShellRiskLevel
    risk_reasons: list[str] = Field(default_factory=list)
    required_capability: str = "diagnostics"
    normalized_action: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_chars: int = 0
    approval_required: bool = False
    approval_granted: bool = False
    approval_id: str | None = None
    error_code: RemoteShellErrorCode | None = None
    message: str | None = None
    reason: str = ""
    execution_success: bool | None = None
    effect_verified: bool | None = None
    verification_status: str = VerificationStatus.NOT_REQUIRED.value


class RemoteHistoryRecord(BaseModel):
    id: str
    timestamp: datetime
    agent_run_id: str | None = None
    host: str
    address: str
    command: str
    risk_level: ShellRiskLevel
    exit_code: int | None = None
    duration_ms: float
    success: bool
    timed_out: bool
    approval_required: bool
    approval_granted: bool
    reason: str = ""
