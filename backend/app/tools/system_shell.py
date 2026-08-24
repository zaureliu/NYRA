from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from app.agent.context import current_agent_run_id
from app.core.config import Settings
from app.core.turn import current_turn_id
from app.events import EventBus, EventType
from app.tools.elevated_broker import is_access_denied_output, process_is_elevated
from app.tools.grounding import VerificationStatus
from app.tools.redaction import redact_secrets
from app.tools.shell_approval import ApprovalRecord, ShellApprovalGate
from app.tools.shell_executor import ShellExecutor, decode_output
from app.tools.shell_history import ShellHistory
from app.tools.shell_models import (
    RiskAssessment,
    ShellErrorCode,
    ShellExecutionResult,
    ShellRiskLevel,
)
from app.tools.shell_risk import ShellRiskClassifier


logger = logging.getLogger("nyra.shell")


class SystemShellService:
    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        *,
        executor: ShellExecutor | None = None,
        classifier: ShellRiskClassifier | None = None,
        approval_gate: ShellApprovalGate | None = None,
        history: ShellHistory | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.executor = executor or ShellExecutor()
        self.classifier = classifier or ShellRiskClassifier()
        self.approvals = approval_gate or ShellApprovalGate(settings.shell_approval_ttl_seconds)
        self.history = history or ShellHistory(settings.database_path)

    async def initialize(self) -> None:
        await self.history.initialize()

    def status(self) -> dict[str, Any]:
        default_executable = self.executor.resolve_executable(self.settings.shell_default)
        return {
            "enabled": self.settings.shell_enabled,
            "default_shell": self.settings.shell_default,
            "default_executable": default_executable,
            "default_working_directory": str(self.settings.shell_default_working_directory),
            "timeout_seconds": self.settings.shell_timeout_seconds,
            "max_timeout_seconds": self.settings.shell_max_timeout_seconds,
            "max_output_chars": self.settings.shell_max_output_chars,
            "max_calls_per_turn": self.settings.shell_max_calls_per_turn,
            "confirm_destructive": self.settings.shell_confirm_destructive,
            "pending_approvals": self.approvals.pending(),
        }

    def preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command", ""))
        shell = str(payload.get("shell") or self.settings.shell_default)
        assessment = self.classifier.classify(command, shell)
        resource = "local:system"
        service = re.search(r"(?i)\b(?:restart-service|stop-service|start-service)\s+['\"]?([A-Za-z0-9_.@-]+)", command)
        container = re.search(r"(?i)\bdocker(?:\.exe)?(?:\s+compose)?\s+(?:restart|stop|start|up|down)\s+([A-Za-z0-9_.@/-]+)", command)
        if service:
            resource = f"local:service:{service.group(1).casefold()}"
        elif container:
            resource = f"local:container:{container.group(1).casefold()}"
        return {"risk_level": assessment.level.value, "resource_key": resource, "host": "local"}

    async def resolve_user_approval(self, text: str) -> ApprovalRecord | None:
        record = self.approvals.resolve_user_statement(text)
        if record:
            approved = record.status == "GRANTED"
            await self.event_bus.publish(
                EventType.SHELL_APPROVAL_DECIDED,
                approval_id=record.approval_id,
                approved=approved,
                command=redact_secrets(record.command),
                risk_level=self._risk_level_value(record),
                turn_id=current_turn_id.get(),
            )
            self._audit(
                "shell_approval_decided",
                command=record.command,
                shell=record.shell,
                working_directory=record.working_directory,
                risk_level=record.risk_level,
                approval_required=True,
                approval_granted=approved,
                source="operator_conversation",
            )
        return record

    @staticmethod
    def _risk_level_value(record: ApprovalRecord) -> str:
        """Approvals de shell guardam enum RiskLevel; os de desktop/fs guardam
        string crua. Normalizar para str em todos os eventos/auditoria."""
        risk = record.risk_level
        return getattr(risk, "value", str(risk))

    async def decide_approval(self, approval_id: str, approved: bool) -> dict[str, Any] | None:
        record = (
            self.approvals.grant(approval_id, "operator_api")
            if approved
            else self.approvals.deny(approval_id, "operator_api")
        )
        if not record:
            return None
        await self.event_bus.publish(
            EventType.SHELL_APPROVAL_DECIDED,
            approval_id=record.approval_id,
            approved=approved,
            command=redact_secrets(record.command),
            risk_level=self._risk_level_value(record),
            turn_id=current_turn_id.get(),
        )
        self._audit(
            "shell_approval_decided",
            command=record.command,
            shell=record.shell,
            working_directory=record.working_directory,
            risk_level=record.risk_level,
            approval_required=True,
            approval_granted=approved,
        )
        return record.public_dict()

    async def execute(
        self,
        command: str,
        shell: str | None = None,
        timeout_seconds: int | None = None,
        working_directory: str | None = None,
        approval_id: str | None = None,
        reason: str = "",
        elevate: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        selected_shell = self.settings.shell_default if shell is None else shell
        timeout = self.settings.shell_timeout_seconds if timeout_seconds is None else timeout_seconds
        cwd_value = str(self.settings.shell_default_working_directory) if working_directory is None else working_directory
        safe_command = redact_secrets(command.strip())
        safe_reason = redact_secrets(reason.strip())
        assessment = self.classifier.classify(command, selected_shell)
        if elevate:
            # Elevação nunca reduz o risco: exige o mesmo approval de qualquer ação sensível.
            from app.tools.shell_models import ShellRiskLevel as _R

            if assessment.level.value in {_R.READ_ONLY.value, _R.LOW_RISK.value}:
                assessment = RiskAssessment(
                    level=ShellRiskLevel.ELEVATED,
                    reasons=[*(assessment.reasons or []), "elevação solicitada (UAC consent)"],
                    components=list(assessment.components or []),
                )

        if not self.settings.shell_enabled:
            return self._error(
                ShellErrorCode.SHELL_DISABLED,
                "O shell local está desabilitado por configuração.",
                safe_command,
                selected_shell,
                cwd_value,
                assessment,
                started,
                safe_reason,
            )
        if not command.strip():
            return self._error(
                ShellErrorCode.INVALID_COMMAND,
                "O comando não pode ser vazio.",
                safe_command,
                selected_shell,
                cwd_value,
                assessment,
                started,
                safe_reason,
            )
        if selected_shell not in {"powershell", "cmd"}:
            return self._error(
                ShellErrorCode.INVALID_COMMAND,
                f"Shell não suportado: {selected_shell}.",
                safe_command,
                "powershell",
                cwd_value,
                assessment,
                started,
                safe_reason,
            )
        if not 1 <= timeout <= self.settings.shell_max_timeout_seconds:
            return self._error(
                ShellErrorCode.INVALID_COMMAND,
                f"Timeout deve ficar entre 1 e {self.settings.shell_max_timeout_seconds} segundos.",
                safe_command,
                selected_shell,
                cwd_value,
                assessment,
                started,
                safe_reason,
            )
        try:
            cwd = Path(cwd_value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return self._error(
                ShellErrorCode.INVALID_WORKING_DIRECTORY,
                "O diretório de trabalho não existe ou não pode ser resolvido.",
                safe_command,
                selected_shell,
                cwd_value,
                assessment,
                started,
                safe_reason,
            )
        if not cwd.is_dir():
            return self._error(
                ShellErrorCode.INVALID_WORKING_DIRECTORY,
                "O working_directory informado não é um diretório.",
                safe_command,
                selected_shell,
                str(cwd),
                assessment,
                started,
                safe_reason,
            )

        approval_required = assessment.level in {
            ShellRiskLevel.ELEVATED,
            ShellRiskLevel.DESTRUCTIVE,
            ShellRiskLevel.CRITICAL,
        }
        approval_granted = False
        agent_run_id = current_agent_run_id.get()
        turn_id = current_turn_id.get()
        if elevate and not self.settings.elevated_broker_enabled:
            return self._error(
                ShellErrorCode.ELEVATION_DISABLED,
                "O Elevated Operations Broker está desabilitado por configuração; nenhuma elevação foi solicitada.",
                safe_command,
                selected_shell,
                cwd_value,
                assessment,
                started,
                safe_reason,
                approval_required=True,
            )
        if approval_required:
            fingerprint = self.approvals.fingerprint(
                command, selected_shell, str(cwd), timeout,
                target="local", agent_run_id=agent_run_id,
            )
            if approval_id:
                approval_granted, rejection = self.approvals.consume(approval_id, fingerprint)
                if not approval_granted:
                    return self._error(
                        ShellErrorCode.COMMAND_REJECTED,
                        rejection,
                        safe_command,
                        selected_shell,
                        str(cwd),
                        assessment,
                        started,
                        safe_reason,
                        approval_required=True,
                        approval_id=approval_id,
                    )
            else:
                if not self.settings.shell_confirm_destructive:
                    return self._error(
                        ShellErrorCode.COMMAND_REJECTED,
                        "Ações sensíveis estão bloqueadas porque o fluxo de confirmação está desabilitado.",
                        safe_command,
                        selected_shell,
                        str(cwd),
                        assessment,
                        started,
                        safe_reason,
                        approval_required=True,
                    )
                record = self.approvals.request(
                    command=command,
                    shell=selected_shell,
                    working_directory=str(cwd),
                    timeout_seconds=timeout,
                    risk_level=assessment.level,
                    target="local",
                    agent_run_id=agent_run_id,
                )
                await self.event_bus.publish(
                    EventType.SHELL_APPROVAL_REQUIRED,
                    approval_id=record.approval_id,
                    agent_run_id=agent_run_id,
                    command=safe_command,
                    shell=selected_shell,
                    working_directory=str(cwd),
                    risk_level=assessment.level.value,
                    reason=assessment.reasons[0] if assessment.reasons else "sensitive command",
                    turn_id=turn_id,
                )
                self._audit(
                    "shell_approval_required",
                    command=command,
                    shell=selected_shell,
                    working_directory=str(cwd),
                    risk_level=assessment.level,
                    approval_required=True,
                    approval_granted=False,
                    reason=safe_reason,
                    agent_run_id=agent_run_id,
                )
                return ShellExecutionResult(
                    success=False,
                    command=safe_command,
                    shell=selected_shell,
                    working_directory=str(cwd),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    risk_level=assessment.level,
                    risk_reasons=assessment.reasons,
                    approval_required=True,
                    approval_granted=False,
                    approval_id=record.approval_id,
                    error_code=ShellErrorCode.APPROVAL_REQUIRED,
                    message="Este comando exige autorização explícita e vinculada antes da execução.",
                    reason=safe_reason,
                ).model_dump(mode="json")

        execution_id = uuid4().hex
        await self.event_bus.publish(
            EventType.SHELL_EXECUTION_STARTED,
            execution_id=execution_id,
            agent_run_id=agent_run_id,
            turn_id=turn_id,
            command=safe_command,
            shell=selected_shell,
            working_directory=str(cwd),
            risk_level=assessment.level.value,
            reason=safe_reason,
        )
        if elevate and approval_granted:
            from app.tools.elevated_broker import run_elevated

            raw = run_elevated(command, selected_shell, timeout, cwd)
            if raw.get("launch_error") == "UAC_CANCELLED":
                return ShellExecutionResult(
                    success=False,
                    execution_id=execution_id,
                    command=safe_command,
                    shell=selected_shell,
                    working_directory=str(cwd),
                    duration_ms=raw["duration_ms"],
                    risk_level=assessment.level,
                    risk_reasons=assessment.reasons,
                    approval_required=True,
                    approval_granted=True,
                    approval_id=approval_id,
                    error_code=ShellErrorCode.ELEVATION_CANCELLED,
                    message="O consentimento UAC não foi concedido pelo operador; o comando não foi executado.",
                    reason=safe_reason,
                ).model_dump(mode="json")
        else:
            raw = await self.executor.execute(command, selected_shell, timeout, cwd)
        stdout = redact_secrets(decode_output(raw.stdout))
        stderr = redact_secrets(decode_output(raw.stderr))
        stdout, stderr, stdout_truncated, stderr_truncated = self._truncate(stdout, stderr)
        powershell_error = bool(
            selected_shell == "powershell"
            and stderr.strip()
            and re.search(r"(?i)(?:CategoryInfo\s*:|FullyQualifiedErrorId\s*:|Acesso negado|Access denied|PermissionDenied)", stderr)
        )
        filter_no_match = bool(
            raw.exit_code == 1
            and not stdout.strip()
            and not stderr.strip()
            and re.search(r"(?i)(?:\bfindstr\b|\bgrep\b)", command)
        )
        success = (raw.exit_code == 0 or filter_no_match) and not raw.timed_out and not raw.launch_error and not powershell_error
        error_code = None
        message = None
        if raw.timed_out:
            error_code = ShellErrorCode.EXECUTION_TIMEOUT
            message = "O comando ultrapassou o timeout e o processo foi encerrado."
        elif raw.launch_error == "UAC_CANCELLED":
            error_code = ShellErrorCode.ELEVATION_CANCELLED
            message = "Consentimento UAC não concedido; comando não executado."
        elif raw.launch_error:
            error_code = ShellErrorCode.EXECUTION_FAILED
            message = redact_secrets(raw.launch_error)
        elif powershell_error:
            error_code = ShellErrorCode.EXECUTION_FAILED
            message = "O PowerShell emitiu um erro não terminante apesar do exit code 0; use stderr e tente um fallback read-only."
        elif raw.exit_code != 0 and not filter_no_match:
            error_code = ShellErrorCode.EXECUTION_FAILED
            message = f"O comando terminou com exit code {raw.exit_code}."
        elif filter_no_match:
            message = "O filtro terminou sem linhas correspondentes; não há listener/processo correspondente neste instante."
        elif stdout or stderr:
            message = "O comando terminou com sucesso; responda usando stdout/stderr reais abaixo."
        else:
            message = "O comando terminou com sucesso e não retornou linhas correspondentes. Não infira erro de permissão."
        if (
            not success
            and not elevate
            and is_access_denied_output(stdout, stderr)
            and not process_is_elevated()
        ):
            from app.tools.shell_models import ShellRiskLevel as _R

            needs_elevation = assessment.level in {
                _R.ELEVATED, _R.DESTRUCTIVE, _R.CRITICAL,
            } or re.search(
                r"(?i)\b(sc(\.exe)?\s|net\s+(?:start|stop)|restart-service|stop-service|start-service|"
                r"set-service|new-netfirewallrule|set-netfirewallrule|netsh\s+advfirewall|"
                r"enable-netadapter|disable-netadapter|bcdedit|reg(?:\.exe)?\s+add|dism|"
                r"add-appxprovisionedpackage|install-windowsfeature)\b",
                command,
            )
            if needs_elevation:
                return ShellExecutionResult(
                    success=False,
                    execution_id=execution_id,
                    command=safe_command,
                    shell=selected_shell,
                    working_directory=str(cwd),
                    duration_ms=raw.duration_ms,
                    risk_level=_R.ELEVATED,
                    risk_reasons=[*(assessment.reasons or []), "operação exige privilégio administrativo"],
                    stdout_truncated=False,
                    stderr_truncated=False,
                    output_chars=len(stdout) + len(stderr),
                    approval_required=True,
                    approval_granted=False,
                    error_code=ShellErrorCode.ELEVATION_REQUIRED,
                    message=(
                        "O Windows indicou que esta operação requer elevação administrativa. "
                        "Solicite approval e, com ele concedido, reenvie o comando com elevate=true; "
                        "o consentimento UAC será exibido ao operador. Nenhuma senha é coletada pela NYRA."
                    ),
                    reason=safe_reason,
                    execution_success=False,
                    effect_verified=False,
                    verification_status=VerificationStatus.EXECUTION_FAILED.value,
                    detail={"elevation": "REQUIRED", "stdout": stdout[:2000], "stderr": stderr[:2000]},
                ).model_dump(mode="json")
        result = ShellExecutionResult(
            success=success,
            execution_id=execution_id,
            command=safe_command,
            shell=selected_shell,
            executable=raw.executable,
            working_directory=str(cwd),
            exit_code=raw.exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=raw.duration_ms,
            timed_out=raw.timed_out,
            risk_level=assessment.level,
            risk_reasons=assessment.reasons,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            output_chars=len(stdout) + len(stderr),
            approval_required=approval_required,
            approval_granted=approval_granted,
            approval_id=approval_id if approval_granted else None,
            error_code=error_code,
            message=message,
            reason=safe_reason,
            execution_success=success,
            effect_verified=None,
            verification_status=(
                VerificationStatus.EXECUTION_FAILED.value if not success
                else VerificationStatus.EXECUTED.value
                if assessment.level != ShellRiskLevel.READ_ONLY
                else VerificationStatus.NOT_REQUIRED.value
            ),
        )
        timestamp = datetime.now(timezone.utc)
        await self.history.add(result, timestamp)
        self._audit(
            "shell_executed",
            execution_id=execution_id,
            command=command,
            shell=selected_shell,
            working_directory=str(cwd),
            risk_level=assessment.level,
            exit_code=raw.exit_code,
            duration_ms=raw.duration_ms,
            timeout=raw.timed_out,
            approval_required=approval_required,
            approval_granted=approval_granted,
            reason=safe_reason,
            agent_run_id=agent_run_id,
        )
        await self.event_bus.publish(
            EventType.SHELL_EXECUTION_FINISHED,
            execution_id=execution_id,
            agent_run_id=agent_run_id,
            turn_id=turn_id,
            command=safe_command,
            shell=selected_shell,
            risk_level=assessment.level.value,
            exit_code=raw.exit_code,
            duration_ms=raw.duration_ms,
            timed_out=raw.timed_out,
            success=success,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
        return result.model_dump(mode="json")

    def _truncate(self, stdout: str, stderr: str) -> tuple[str, str, bool, bool]:
        limit = self.settings.shell_max_output_chars
        if len(stdout) + len(stderr) <= limit:
            return stdout, stderr, False, False
        if stdout and stderr:
            stdout_limit = max(1, int(limit * 0.75))
            stderr_limit = max(1, limit - stdout_limit)
        elif stdout:
            stdout_limit, stderr_limit = limit, 0
        else:
            stdout_limit, stderr_limit = 0, limit
        stdout_value, stdout_truncated = self._truncate_one(stdout, stdout_limit)
        stderr_value, stderr_truncated = self._truncate_one(stderr, stderr_limit)
        return stdout_value, stderr_value, stdout_truncated, stderr_truncated

    @staticmethod
    def _truncate_one(value: str, limit: int) -> tuple[str, bool]:
        if len(value) <= limit:
            return value, False
        marker = f"\n\n... [NYRA OUTPUT TRUNCATED: {len(value) - limit} characters omitted] ...\n\n"
        available = max(0, limit - len(marker))
        head = int(available * 0.6)
        tail = available - head
        return value[:head] + marker + (value[-tail:] if tail else ""), True

    @staticmethod
    def _error(
        code: ShellErrorCode,
        message: str,
        command: str,
        shell: str,
        working_directory: str,
        assessment: RiskAssessment,
        started: float,
        reason: str,
        *,
        approval_required: bool = False,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        return ShellExecutionResult(
            success=False,
            command=command,
            shell="cmd" if shell == "cmd" else "powershell",
            working_directory=working_directory,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            risk_level=assessment.level,
            risk_reasons=assessment.reasons,
            approval_required=approval_required,
            approval_id=approval_id,
            error_code=code,
            message=message,
            reason=reason,
        ).model_dump(mode="json")

    @staticmethod
    def _audit(event: str, **fields: Any) -> None:
        safe = {
            key: (
                value.value
                if isinstance(value, ShellRiskLevel)
                else redact_secrets(value)
                if isinstance(value, str)
                else value
            )
            for key, value in fields.items()
        }
        logger.info(event, extra=safe)
