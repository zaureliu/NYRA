from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.tools.grounding import VerificationStatus


class ShellRiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    ELEVATED = "ELEVATED"
    DESTRUCTIVE = "DESTRUCTIVE"
    CRITICAL = "CRITICAL"


class ShellErrorCode(StrEnum):
    SHELL_DISABLED = "SHELL_DISABLED"
    INVALID_COMMAND = "INVALID_COMMAND"
    INVALID_WORKING_DIRECTORY = "INVALID_WORKING_DIRECTORY"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    SHELL_CALL_LIMIT = "SHELL_CALL_LIMIT"
    ELEVATION_REQUIRED = "ELEVATION_REQUIRED"
    ELEVATION_CANCELLED = "ELEVATION_CANCELLED"
    ELEVATION_DISABLED = "ELEVATION_DISABLED"


class ShellExecuteInput(BaseModel):
    command: str = Field(
        max_length=32_768,
        description="Comando completo a executar localmente no shell selecionado.",
    )
    shell: Literal["powershell", "cmd"] | None = Field(
        default=None,
        description="Shell local. O padrão configurado é PowerShell.",
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        le=3_600,
        description="Timeout para comandos finitos; processos persistentes não devem usar esta tool.",
    )
    working_directory: str | None = Field(
        default=None,
        max_length=2_048,
        description="Diretório local existente. O padrão é a raiz do projeto KAZUMI.",
    )
    approval_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        description="ID de aprovação emitido e concedido pelo backend para este comando exato.",
    )
    elevate: bool = Field(
        default=False,
        description=(
            "Quando true, reenvia o comando já aprovado através do Elevated Operations Broker "
            "com -Verb RunAs. O Windows exibe o consentimento UAC ao operador; nenhuma senha é "
            "coletada ou armazenada pela KAZUMI."
        ),
    )
    reason: str = Field(
        default="",
        max_length=500,
        description="Motivo curto e verificável para a execução, usado na auditoria.",
    )


class RiskAssessment(BaseModel):
    level: ShellRiskLevel
    reasons: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)


class ShellExecutionResult(BaseModel):
    success: bool
    execution_id: str | None = None
    command: str
    shell: Literal["powershell", "cmd"]
    executable: str | None = None
    working_directory: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0
    timed_out: bool = False
    risk_level: ShellRiskLevel
    risk_reasons: list[str] = Field(default_factory=list)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_chars: int = 0
    approval_required: bool = False
    approval_granted: bool = False
    approval_id: str | None = None
    error_code: ShellErrorCode | None = None
    message: str | None = None
    reason: str = ""
    detail: dict | None = None
    execution_success: bool | None = None
    effect_verified: bool | None = None
    verification_status: str = VerificationStatus.NOT_REQUIRED.value


class ShellHistoryRecord(BaseModel):
    id: str
    timestamp: datetime
    command: str
    working_directory: str
    shell: str
    risk_level: ShellRiskLevel
    exit_code: int | None = None
    duration_ms: float
    success: bool
    timed_out: bool
    approval_required: bool
    approval_granted: bool
    reason: str = ""


class ShellApprovalDecision(BaseModel):
    approved: bool

