from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import shlex
import time
from typing import Any
from uuid import uuid4

from app.agent.context import current_agent_run_id
from app.core.config import Settings
from app.core.turn import current_turn_id
from app.events import EventBus, EventType
from app.network_aliases import NetworkAliasRegistry, NetworkHostAlias
from app.tools.redaction import redact_secrets
from app.tools.grounding import VerificationStatus
from app.tools.remote_executor import OpenSSHExecutor, RawRemoteResult
from app.tools.remote_history import RemoteShellHistory
from app.tools.remote_models import (
    RemoteExecutionResult,
    RemotePolicyAssessment,
    RemoteShellErrorCode,
)
from app.tools.remote_policy import RemoteCommandPolicy
from app.tools.shell_approval import ShellApprovalGate
from app.tools.shell_executor import decode_output
from app.tools.shell_models import RiskAssessment, ShellRiskLevel


logger = logging.getLogger("nyra.remote_shell")


class RemoteShellService:
    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        approvals: ShellApprovalGate,
        *,
        hosts: NetworkAliasRegistry | None = None,
        executor: OpenSSHExecutor | None = None,
        policy: RemoteCommandPolicy | None = None,
        history: RemoteShellHistory | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.approvals = approvals
        self.hosts = hosts or NetworkAliasRegistry(settings.trusted_hosts_path)
        self.executor = executor or OpenSSHExecutor()
        self.policy = policy or RemoteCommandPolicy()
        self.history = history or RemoteShellHistory(settings.database_path)

    async def initialize(self) -> None:
        await self.history.initialize()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.remote_shell_enabled,
            "client": self.executor.resolve_executable(),
            "connect_timeout_seconds": self.settings.ssh_connect_timeout_seconds,
            "command_timeout_seconds": self.settings.ssh_command_timeout_seconds,
            "max_timeout_seconds": self.settings.ssh_max_timeout_seconds,
            "max_output_chars": self.settings.ssh_max_output_chars,
            "hosts": self.hosts.public_remote_hosts(),
        }

    def prompt_summary(self) -> str:
        return self.hosts.remote_prompt_summary()

    def preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        alias = str(payload.get("host", ""))
        host = self.hosts.resolve_remote(alias)
        if host is None:
            return {"risk_level": ShellRiskLevel.ELEVATED.value, "resource_key": "remote:unknown"}
        assessment = self._assess(host, str(payload.get("command", "")))
        resource = assessment.resource_name or host.id
        return {
            "risk_level": assessment.risk_level.value,
            "resource_key": f"remote:{host.id}:{assessment.resource_type or 'host'}:{resource}",
            "host": host.id,
            "address": host.address,
            "required_capability": assessment.required_capability,
            "normalized_action": assessment.normalized_action,
            "auto_remediation_allowed": assessment.auto_remediation_allowed,
        }

    async def execute(
        self,
        host: str,
        command: str,
        timeout_seconds: int | None = None,
        working_directory: str | None = None,
        approval_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        timeout = self.settings.ssh_command_timeout_seconds if timeout_seconds is None else timeout_seconds
        safe_command = redact_secrets(command.strip())
        safe_reason = redact_secrets(reason.strip())
        logical = self.hosts.resolve_remote(host)
        fallback_assessment = RemotePolicyAssessment(
            risk_level=ShellRiskLevel.ELEVATED,
            reasons=["host has not been resolved"],
        )
        if not self.settings.remote_shell_enabled:
            return self._error(RemoteShellErrorCode.REMOTE_SHELL_DISABLED, "SSH remoto está desabilitado.", host, "", command, fallback_assessment, started, safe_reason)
        if logical is None:
            return self._error(RemoteShellErrorCode.UNKNOWN_TRUSTED_HOST, "Host não pertence ao Trusted Host Registry.", host, "", command, fallback_assessment, started, safe_reason)
        remote = logical.remote_shell
        assessment = self._assess(logical, command)
        if not remote.enabled:
            return self._error(RemoteShellErrorCode.HOST_DISABLED, "SSH está desabilitado para este host cadastrado.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical)
        if not remote.username:
            return self._error(RemoteShellErrorCode.SSH_CREDENTIALS_MISSING, "O host não possui usuário SSH cadastrado.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical)
        if not command.strip():
            return self._error(RemoteShellErrorCode.INVALID_COMMAND, "O comando remoto não pode ser vazio.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical)
        if re.search(r"(?i)\b(?:ssh|scp|sftp|sshpass|plink|pscp|winrm|psexec)(?:\.exe)?\b", command):
            return self._error(
                RemoteShellErrorCode.COMMAND_REJECTED,
                "Encadear outro transporte remoto dentro de remote_shell é proibido.",
                logical.id, logical.address, safe_command, assessment, started, safe_reason, logical,
            )
        if not 1 <= timeout <= self.settings.ssh_max_timeout_seconds:
            return self._error(RemoteShellErrorCode.INVALID_COMMAND, f"Timeout deve ficar entre 1 e {self.settings.ssh_max_timeout_seconds} segundos.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical)
        if working_directory and ("\x00" in working_directory or "\r" in working_directory or "\n" in working_directory):
            return self._error(RemoteShellErrorCode.INVALID_WORKING_DIRECTORY, "Diretório remoto inválido.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical)
        if assessment.required_capability not in remote.capabilities:
            return self._error(
                RemoteShellErrorCode.CAPABILITY_DENIED,
                f"O host não permite a capability {assessment.required_capability}.",
                logical.id, logical.address, safe_command, assessment, started, safe_reason, logical,
            )
        agent_run_id = current_agent_run_id.get()
        turn_id = current_turn_id.get()
        if agent_run_id and self.settings.agent_read_only and assessment.risk_level != ShellRiskLevel.READ_ONLY:
            return self._error(RemoteShellErrorCode.AGENT_READ_ONLY, "O Agent Loop está em modo somente leitura.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical, agent_run_id=agent_run_id)

        known_hosts = remote.resolve_path(remote.known_hosts_path)
        private_key = remote.resolve_path(remote.private_key_path)
        if known_hosts is None or not self._is_file(known_hosts):
            return self._error(RemoteShellErrorCode.SSH_KNOWN_HOSTS_MISSING, "Arquivo known_hosts cadastrado não está disponível; a conexão foi bloqueada.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical, agent_run_id=agent_run_id)
        if private_key is not None and not self._is_file(private_key):
            return self._error(RemoteShellErrorCode.SSH_CREDENTIALS_MISSING, "A chave privada cadastrada não está disponível.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical, agent_run_id=agent_run_id)
        if private_key is None and not remote.use_ssh_agent:
            return self._error(RemoteShellErrorCode.SSH_CREDENTIALS_MISSING, "O host não possui chave privada nem SSH agent autorizado.", logical.id, logical.address, safe_command, assessment, started, safe_reason, logical, agent_run_id=agent_run_id)

        approval_required = assessment.risk_level in {
            ShellRiskLevel.ELEVATED, ShellRiskLevel.DESTRUCTIVE, ShellRiskLevel.CRITICAL,
        } and not assessment.auto_remediation_allowed
        approval_granted = False
        target = f"remote:{logical.id}"
        cwd_context = working_directory or ""
        if approval_required:
            fingerprint = self.approvals.fingerprint(
                command, "ssh", cwd_context, timeout, target=target, agent_run_id=agent_run_id,
            )
            if approval_id:
                approval_granted, rejection = self.approvals.consume(approval_id, fingerprint)
                if not approval_granted:
                    return self._error(RemoteShellErrorCode.COMMAND_REJECTED, rejection, logical.id, logical.address, safe_command, assessment, started, safe_reason, logical, approval_required=True, approval_id=approval_id, agent_run_id=agent_run_id)
            else:
                record = self.approvals.request(
                    command=command, shell="ssh", working_directory=cwd_context,
                    timeout_seconds=timeout, risk_level=assessment.risk_level,
                    target=target, agent_run_id=agent_run_id,
                )
                await self.event_bus.publish(
                    EventType.REMOTE_SHELL_APPROVAL_REQUIRED,
                    approval_id=record.approval_id, agent_run_id=agent_run_id,
                    host=logical.id, command=safe_command, risk_level=assessment.risk_level.value,
                    reason=assessment.reasons[0] if assessment.reasons else "remote state change",
                    turn_id=turn_id,
                )
                self._audit("remote_approval_required", logical, command, assessment, safe_reason, agent_run_id=agent_run_id, approval_required=True, approval_granted=False)
                return self._error(
                    RemoteShellErrorCode.APPROVAL_REQUIRED,
                    "Esta ação remota requer autorização explícita vinculada ao comando.",
                    logical.id, logical.address, safe_command, assessment, started, safe_reason, logical,
                    approval_required=True, approval_id=record.approval_id, agent_run_id=agent_run_id,
                )

        execution_id = uuid4().hex
        remote_command = command if not working_directory else f"cd {shlex.quote(working_directory)} && {command}"
        await self.event_bus.publish(
            EventType.REMOTE_SHELL_EXECUTION_STARTED,
            execution_id=execution_id, agent_run_id=agent_run_id, host=logical.id,
            command=safe_command, risk_level=assessment.risk_level.value,
            turn_id=turn_id,
        )
        raw = await self.executor.execute(
            logical, remote_command,
            connect_timeout_seconds=self.settings.ssh_connect_timeout_seconds,
            command_timeout_seconds=timeout,
            known_hosts_path=known_hosts,
            private_key_path=private_key,
        )
        result = self._result_from_raw(
            raw, logical, safe_command, working_directory, assessment, safe_reason,
            execution_id, agent_run_id, approval_required, approval_granted,
        )
        await self.history.add(result, datetime.now(timezone.utc))
        await self.event_bus.publish(
            EventType.REMOTE_SHELL_EXECUTION_FINISHED,
            execution_id=execution_id, agent_run_id=agent_run_id, host=logical.id,
            command=safe_command, risk_level=assessment.risk_level.value,
            success=result.success, exit_code=result.exit_code, duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            turn_id=turn_id,
        )
        self._audit(
            "remote_shell_executed", logical, command, assessment, safe_reason,
            agent_run_id=agent_run_id, exit_code=result.exit_code,
            duration_ms=result.duration_ms, timed_out=result.timed_out,
            approval_required=approval_required, approval_granted=approval_granted,
        )
        return result.model_dump(mode="json")

    def _assess(self, host: NetworkHostAlias, command: str) -> RemotePolicyAssessment:
        actions = {item.strip() for item in self.settings.agent_auto_remediation_actions.split(",") if item.strip()}
        return self.policy.assess(
            host, command,
            auto_remediation_enabled=self.settings.agent_auto_remediation,
            global_actions=actions,
        )

    def _result_from_raw(
        self,
        raw: RawRemoteResult,
        host: NetworkHostAlias,
        command: str,
        working_directory: str | None,
        assessment: RemotePolicyAssessment,
        reason: str,
        execution_id: str,
        agent_run_id: str | None,
        approval_required: bool,
        approval_granted: bool,
    ) -> RemoteExecutionResult:
        stdout = redact_secrets(decode_output(raw.stdout))
        stderr = redact_secrets(decode_output(raw.stderr))
        stdout, stdout_truncated = self._truncate(stdout)
        stderr, stderr_truncated = self._truncate(stderr)
        error_code: RemoteShellErrorCode | None = None
        message: str | None = None
        evidence = f"{stderr}\n{raw.launch_error or ''}".casefold()
        if raw.timed_out:
            error_code, message = RemoteShellErrorCode.SSH_COMMAND_TIMEOUT, "O comando SSH excedeu o timeout e a sessão controlada foi encerrada."
        elif raw.launch_error:
            error_code, message = RemoteShellErrorCode.SSH_CLIENT_UNAVAILABLE, "O cliente SSH não pôde ser iniciado."
        elif re.search(r"remote host identification has changed|host key verification failed|offending .* key", evidence):
            error_code, message = RemoteShellErrorCode.SSH_HOST_KEY_MISMATCH, "A identidade SSH do host mudou ou não pôde ser verificada; conexão bloqueada."
        elif re.search(r"permission denied|authentication failed|no supported authentication", evidence):
            error_code, message = RemoteShellErrorCode.SSH_AUTHENTICATION_FAILED, "Autenticação SSH rejeitada."
        elif re.search(r"connection timed out|operation timed out|connect to host .* timed out", evidence):
            error_code, message = RemoteShellErrorCode.SSH_CONNECTION_TIMEOUT, "A conexão SSH excedeu o timeout."
        elif raw.exit_code == 255:
            error_code, message = RemoteShellErrorCode.SSH_CONNECTION_FAILED, "A conexão SSH falhou."
        filter_no_match = raw.exit_code == 1 and not stdout and not stderr and bool(re.search(r"(?:^|[|;&]\s*)(?:grep|egrep|fgrep)\b", command, re.I))
        success = (raw.exit_code == 0 or filter_no_match) and error_code is None
        if not success and error_code is None:
            error_code = RemoteShellErrorCode.SSH_COMMAND_FAILED
            message = f"O comando remoto terminou com exit code {raw.exit_code}."
        if (stdout_truncated or stderr_truncated) and success:
            message = "Saída truncada pelo limite configurado."
        return RemoteExecutionResult(
            success=success, execution_id=execution_id, agent_run_id=agent_run_id,
            host=host.id, address=host.address, port=host.remote_shell.port,
            platform=host.remote_shell.platform, command=command,
            working_directory=working_directory, exit_code=raw.exit_code,
            stdout=stdout, stderr=stderr, duration_ms=raw.duration_ms,
            timed_out=raw.timed_out, risk_level=assessment.risk_level,
            risk_reasons=assessment.reasons, required_capability=assessment.required_capability,
            normalized_action=assessment.normalized_action,
            stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated,
            output_chars=len(stdout) + len(stderr), approval_required=approval_required,
            approval_granted=approval_granted, error_code=error_code, message=message, reason=reason,
            execution_success=success, effect_verified=None,
            verification_status=(
                VerificationStatus.EXECUTION_FAILED.value if not success
                else VerificationStatus.EXECUTED.value
                if assessment.risk_level != ShellRiskLevel.READ_ONLY
                else VerificationStatus.NOT_REQUIRED.value
            ),
        )

    def _truncate(self, value: str) -> tuple[str, bool]:
        limit = self.settings.ssh_max_output_chars
        if len(value) <= limit:
            return value, False
        marker = "\n...[OUTPUT TRUNCATED BY NYRA]...\n"
        available = max(2, limit - len(marker))
        head = available * 3 // 5
        return value[:head] + marker + value[-(available - head):], True

    @staticmethod
    def _is_file(path: Path) -> bool:
        try:
            return path.is_file()
        except OSError:
            return False

    def _error(
        self,
        code: RemoteShellErrorCode,
        message: str,
        host: str,
        address: str,
        command: str,
        assessment: RemotePolicyAssessment,
        started: float,
        reason: str,
        logical: NetworkHostAlias | None = None,
        *,
        approval_required: bool = False,
        approval_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        result = RemoteExecutionResult(
            success=False, agent_run_id=agent_run_id, host=host, address=address,
            port=logical.remote_shell.port if logical else 22,
            platform=logical.remote_shell.platform if logical else "unknown",
            command=redact_secrets(command), duration_ms=round((time.perf_counter() - started) * 1000, 2),
            risk_level=assessment.risk_level, risk_reasons=assessment.reasons,
            required_capability=assessment.required_capability,
            normalized_action=assessment.normalized_action,
            approval_required=approval_required, approval_id=approval_id,
            error_code=code, message=message, reason=reason,
        )
        return result.model_dump(mode="json")

    @staticmethod
    def _audit(
        event: str,
        host: NetworkHostAlias,
        command: str,
        assessment: RemotePolicyAssessment,
        reason: str,
        **extra: Any,
    ) -> None:
        logger.info(
            event,
            extra={
                "host": host.id,
                "address": host.address,
                "command": redact_secrets(command),
                "risk_level": assessment.risk_level.value,
                "capability": assessment.required_capability,
                "normalized_action": assessment.normalized_action,
                "reason": redact_secrets(reason),
                **extra,
            },
        )
