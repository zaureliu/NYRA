from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import time

from app.tools.redaction import redact_secrets
from app.tools.shell_models import ShellRiskLevel


@dataclass
class ApprovalRecord:
    approval_id: str
    fingerprint: str
    command: str
    shell: str
    working_directory: str
    timeout_seconds: int
    risk_level: ShellRiskLevel
    created_at: float
    expires_at: float
    target: str = "local"
    agent_run_id: str | None = None
    status: str = "PENDING"
    granted_source: str = ""

    def public_dict(self) -> dict:
        # Approvals de shell guardam ShellRiskLevel (enum); os criados pelas
        # tools de desktop/fs guardam string. Normalizar para str sempre.
        risk = self.risk_level
        return {
            "approval_id": self.approval_id,
            "command": redact_secrets(self.command),
            "shell": self.shell,
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "risk_level": getattr(risk, "value", str(risk)),
            "target": self.target,
            "agent_run_id": self.agent_run_id,
            "status": self.status,
            "expires_at": self.expires_at,
            "granted_source": self.granted_source,
        }


class ShellApprovalGate:
    _approve = re.compile(
        r"(?i)^\s*(?:sim(?:,?\s+(?:autorizo|confirmo|pode\s+(?:executar|prosseguir)))?|"
        r"autorizo|confirmo|pode\s+(?:executar|prosseguir)|execute)\s*[.!]?\s*$"
    )
    _deny = re.compile(
        r"(?i)^\s*(?:n[aã]o|nego|cancela|cancelar|n[aã]o\s+(?:execute|autorizo))\s*[.!]?\s*$"
    )

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, ApprovalRecord] = {}

    @staticmethod
    def fingerprint(
        command: str,
        shell: str,
        working_directory: str,
        timeout_seconds: int,
        *,
        target: str = "local",
        agent_run_id: str | None = None,
    ) -> str:
        canonical = json.dumps(
            {
                "command": command.strip(),
                "shell": shell.casefold(),
                "working_directory": working_directory.casefold(),
                "timeout_seconds": timeout_seconds,
                "target": target.casefold(),
                "agent_run_id": agent_run_id or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def request(
        self,
        *,
        command: str,
        shell: str,
        working_directory: str,
        timeout_seconds: int,
        risk_level: ShellRiskLevel,
        target: str = "local",
        agent_run_id: str | None = None,
        fingerprint: str | None = None,
    ) -> ApprovalRecord:
        self._expire()
        # O chamador pode trazer o fingerprint já calculado (mesmo tuple que
        # usará no consume) — garante que registro e consumo sempre coincidam.
        fp = fingerprint or self.fingerprint(
            command, shell, working_directory, timeout_seconds,
            target=target, agent_run_id=agent_run_id,
        )
        for record in self._records.values():
            if record.fingerprint == fp and record.status == "PENDING":
                return record
        now = time.time()
        record = ApprovalRecord(
            approval_id="apr_" + secrets.token_urlsafe(24),
            fingerprint=fp,
            command=command,
            shell=shell,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
            risk_level=risk_level,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            target=target,
            agent_run_id=agent_run_id,
        )
        self._records[record.approval_id] = record
        return record

    def grant(self, approval_id: str, source: str = "operator_api") -> ApprovalRecord | None:
        self._expire()
        record = self._records.get(approval_id)
        if not record or record.status != "PENDING":
            return None
        record.status = "GRANTED"
        record.granted_source = source
        return record

    def deny(self, approval_id: str, source: str = "operator_api") -> ApprovalRecord | None:
        self._expire()
        record = self._records.get(approval_id)
        if not record or record.status not in {"PENDING", "GRANTED"}:
            return None
        record.status = "DENIED"
        record.granted_source = source
        return record

    def consume(self, approval_id: str, fingerprint: str) -> tuple[bool, str]:
        self._expire()
        record = self._records.get(approval_id)
        if not record:
            return False, "Approval ID inexistente ou expirado."
        if not secrets.compare_digest(record.fingerprint, fingerprint):
            return False, "A aprovação pertence a outro comando ou contexto."
        if record.status != "GRANTED":
            return False, f"A aprovação está em estado {record.status}."
        record.status = "CONSUMED"
        return True, ""

    def resolve_user_statement(self, text: str) -> ApprovalRecord | None:
        """Grant/deny only one unambiguous pending request from a strict reply.

        The LLM cannot call this method and cannot turn arbitrary prose into a
        grant. The orchestrator passes the operator's raw turn before tool use.
        """

        pending = self.pending()
        if len(pending) != 1:
            return None
        record = self._records[pending[0]["approval_id"]]
        if self._approve.fullmatch(text):
            if record.status == "GRANTED":
                return record
            return self.grant(record.approval_id, "operator_conversation")
        if self._deny.fullmatch(text):
            return self.deny(record.approval_id, "operator_conversation")
        return None

    def pending(self) -> list[dict]:
        self._expire()
        return [
            record.public_dict()
            for record in self._records.values()
            if record.status in {"PENDING", "GRANTED"}
        ]

    def get(self, approval_id: str) -> ApprovalRecord | None:
        self._expire()
        return self._records.get(approval_id)

    def _expire(self) -> None:
        now = time.time()
        for record in self._records.values():
            if record.status in {"PENDING", "GRANTED"} and record.expires_at <= now:
                record.status = "EXPIRED"
