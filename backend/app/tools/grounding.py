"""Tool grounding: provenance, execution-vs-effect tracking and anti-fabrication checks.

Every tool result observed by the Agent Loop becomes a ToolObservation with explicit
provenance (tool_call_id, execution_id, fingerprints). Mutations start as EXECUTED and
only become VERIFIED when a correlated read-only observation matches their subject.
Draft responses are checked against this ledger so invented values (PID, latency,
exit codes...) or effect claims without verification are caught before reaching the
operator. This is backend enforcement; the system prompt is only a second layer.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.turn import (
    CROSS_TURN_OBSERVATION_REJECTED,
    CrossTurnObservationError,
)

logger = logging.getLogger("kazumi.grounding")

READ_ONLY_RISKS = {"", "READ_ONLY"}

_MAX_EVIDENCE_CHARS = 8000


class VerificationStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


def initial_verification_status(success: bool | None, risk_level: str) -> VerificationStatus:
    if success is False:
        return VerificationStatus.EXECUTION_FAILED
    risk = str(risk_level or "").upper()
    if success is True and risk not in READ_ONLY_RISKS:
        return VerificationStatus.EXECUTED
    return VerificationStatus.NOT_REQUIRED


class ToolObservation(BaseModel):
    """Provenance-bound record of one tool result."""

    tool_call_id: str
    tool_name: str
    execution_id: str | None = None
    agent_run_id: str | None = None
    turn_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    arguments_fingerprint: str = ""
    resource_key: str = ""
    risk_level: str = ""
    ok: bool = False
    success: bool | None = None
    exit_code: int | None = None
    error_code: str | None = None
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    message: str = ""
    structured_evidence: str = ""
    hardware_facts: list[dict] = Field(default_factory=list)
    effect_observed: bool | None = None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    verification_status: VerificationStatus = VerificationStatus.NOT_REQUIRED
    verified_by_call_id: str | None = None


class GroundingViolation(BaseModel):
    kind: str  # FABRICATED_VALUE | TRUNCATED_UNVERIFIABLE | UNVERIFIED_EFFECT
    detail: str


class GroundingLedger:
    """Correlates tool_call_id → observation and tracks mutation verification state.

    The ledger is turn-scoped: every record is stamped with the owning turn_id
    and lookups must present the matching turn_id, otherwise the request is
    rejected with CROSS_TURN_OBSERVATION_REJECTED. A ledger from turn A can
    therefore never satisfy a query issued by turn B.
    """

    def __init__(self, turn_id: str | None = None) -> None:
        self.turn_id = turn_id
        self.observations: list[ToolObservation] = []
        self.by_call_id: dict[str, ToolObservation] = {}
        self.cross_turn_rejections = 0

    @staticmethod
    def new_call_id() -> str:
        return f"call_{uuid4().hex[:12]}"

    def require_turn(self, turn_id: str | None) -> str:
        """Namespace guard: observations are only served to their own turn."""
        if self.turn_id and turn_id != self.turn_id:
            self.cross_turn_rejections += 1
            logger.warning(
                "cross_turn_observation_rejected",
                extra={
                    "ledger_turn_id": self.turn_id,
                    "requested_turn_id": turn_id,
                },
            )
            raise CrossTurnObservationError(
                observation_turn_id=self.turn_id,
                requested_turn_id=turn_id,
                tool_call_id="*",
            )
        return self.turn_id or ""

    def observation(self, tool_call_id: str, *, turn_id: str | None = None) -> ToolObservation:
        """Fetch an observation enforcing turn_id + tool_call_id correlation."""
        if self.turn_id and turn_id is not None and turn_id != self.turn_id:
            self.cross_turn_rejections += 1
            logger.warning(
                "cross_turn_observation_rejected",
                extra={
                    "ledger_turn_id": self.turn_id,
                    "requested_turn_id": turn_id,
                    "tool_call_id": tool_call_id,
                },
            )
            raise CrossTurnObservationError(
                observation_turn_id=self.turn_id,
                requested_turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
        observation = self.by_call_id.get(tool_call_id)
        if observation is not None and self.turn_id and observation.turn_id != self.turn_id:
            raise CrossTurnObservationError(
                observation_turn_id=observation.turn_id,
                requested_turn_id=self.turn_id,
                tool_call_id=tool_call_id,
            )
        return observation

    def record(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        result_data: dict,
        risk_level: str = "READ_ONLY",
        resource_key: str = "",
        arguments_fingerprint: str = "",
        agent_run_id: str | None = None,
        turn_id: str | None = None,
    ) -> ToolObservation:
        data = result_data if isinstance(result_data, dict) else {}
        success_value = data.get("success")
        success = None if success_value is None else bool(success_value)
        exit_code = data.get("exit_code")
        error_code = data.get("error_code")
        structured_evidence, effect_observed = _structured_result_evidence(data)
        observation = ToolObservation(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            execution_id=str(data["execution_id"])[:64] if data.get("execution_id") else None,
            agent_run_id=agent_run_id,
            turn_id=turn_id or self.turn_id,
            arguments_fingerprint=arguments_fingerprint,
            resource_key=resource_key,
            risk_level=str(risk_level or "").upper(),
            ok=bool(data.get("ok", success is not False)),
            success=success,
            exit_code=int(exit_code) if isinstance(exit_code, int) else None,
            error_code=str(error_code)[:64] if error_code else None,
            command=_clean_text(data.get("command"), 2000),
            stdout=_clean_text(data.get("stdout"), _MAX_EVIDENCE_CHARS),
            stderr=_clean_text(data.get("stderr"), _MAX_EVIDENCE_CHARS // 2),
            message=_clean_text(data.get("message"), 2000),
            structured_evidence=structured_evidence,
            hardware_facts=[fact for fact in (data.get("hardware_facts") or [])[:32]
                            if isinstance(fact, dict)] if isinstance(data.get("hardware_facts"), list) else [],
            effect_observed=effect_observed,
            timed_out=bool(data.get("timed_out")),
            stdout_truncated=bool(data.get("stdout_truncated")),
            stderr_truncated=bool(data.get("stderr_truncated")),
            verification_status=initial_verification_status(success, risk_level),
        )
        self.observations.append(observation)
        self.by_call_id[observation.tool_call_id] = observation
        logger.debug(
            "tool_observation_recorded",
            extra={
                "agent_run_id": observation.agent_run_id,
                "turn_id": observation.turn_id,
                "tool": observation.tool_name,
                "tool_call_id": observation.tool_call_id,
                "execution_id": observation.execution_id,
                "risk_level": observation.risk_level,
                "success": observation.success,
                "exit_code": observation.exit_code,
                "verification_status": observation.verification_status.value,
                "stdout_truncated": observation.stdout_truncated,
                "stderr_truncated": observation.stderr_truncated,
            },
        )
        return observation

    def pending_mutations(self) -> list[ToolObservation]:
        return [item for item in self.observations if item.verification_status == VerificationStatus.EXECUTED]

    def record_verification_attempt(self, observation: ToolObservation) -> list[ToolObservation]:
        """Mark EXECUTED mutations VERIFIED when a successful read-only probe follows them.

        Attribution prefers probes sharing a subject token with the mutation command;
        if none matches, any successful read-only probe after the mutation still counts
        as turn-level verification (existing Agent Loop semantics).

        Evidence rules (boilerplate service `message` fields are NOT evidence):
        - empty stdout+stderr            -> VERIFICATION_FAILED (probe confirms nothing)
        - explicit negative ("False"/"0") -> VERIFICATION_FAILED (effect did not occur)
        - any other real output          -> VERIFIED
        """
        matched: list[ToolObservation] = []
        pending = self.pending_mutations()
        if not pending:
            return matched
        if observation.success and observation.risk_level in READ_ONLY_RISKS:
            stdout = observation.stdout.strip()
            stderr = observation.stderr.strip()
            if observation.effect_observed is not None:
                status = (
                    VerificationStatus.VERIFIED
                    if observation.effect_observed
                    else VerificationStatus.VERIFICATION_FAILED
                )
            elif not stdout and not stderr and not observation.structured_evidence:
                status = VerificationStatus.VERIFICATION_FAILED
            elif stdout.casefold() in {"false", "$false", "0"}:
                status = VerificationStatus.VERIFICATION_FAILED
            else:
                status = VerificationStatus.VERIFIED
            subject_matched = [
                candidate for candidate in pending
                if _shares_subject(candidate.command, observation.command)
            ]
            targets = subject_matched or pending
            for candidate in targets:
                candidate.verification_status = status
                candidate.verified_by_call_id = observation.tool_call_id
                matched.append(candidate)
            logger.debug(
                "mutation_verification_matched",
                extra={
                    "verifier_call_id": observation.tool_call_id,
                    "verified_call_ids": [item.tool_call_id for item in matched],
                    "status": status.value,
                    "agent_run_id": observation.agent_run_id,
                },
            )
        else:
            for candidate in pending:
                if _shares_subject(candidate.command, observation.command):
                    candidate.verification_status = VerificationStatus.VERIFICATION_FAILED
                    candidate.verified_by_call_id = observation.tool_call_id
        return matched

    def evidence_text(self) -> str:
        parts: list[str] = []
        for item in self.observations:
            parts.extend((item.command, item.stdout, item.stderr, item.message, item.structured_evidence))
            if item.exit_code is not None:
                parts.append(str(item.exit_code))
            if item.error_code:
                parts.append(item.error_code)
        return "\n".join(parts).casefold()

    def truncated_outputs(self) -> int:
        return sum(1 for item in self.observations if item.stdout_truncated or item.stderr_truncated)

    def has_any_output(self) -> bool:
        return any(
            item.stdout.strip() or item.stderr.strip() or item.message.strip() or item.structured_evidence.strip()
            for item in self.observations
        )


def _structured_result_evidence(data: dict) -> tuple[str, bool | None]:
    """Project safe structured probe fields into grounding evidence."""
    evidence: dict = {}
    effect_observed: bool | None = None
    for key in ("open", "running", "healthy", "ready", "exists", "effect_verified"):
        value = data.get(key)
        if isinstance(value, bool):
            evidence[key] = value
            if effect_observed is None:
                effect_observed = value
    verification_status = data.get("verification_status")
    if isinstance(verification_status, str):
        evidence["verification_status"] = verification_status[:64]
        if effect_observed is None and verification_status.upper() == "VERIFIED":
            effect_observed = True
        elif effect_observed is None and verification_status.upper() in {
            "VERIFICATION_FAILED", "EXECUTION_FAILED",
        }:
            effect_observed = False
    state = data.get("state")
    if isinstance(state, str):
        evidence["state"] = state[:64]
    windows = data.get("windows")
    if isinstance(windows, list):
        evidence["windows"] = [
            {
                key: window.get(key)
                for key in ("pid", "visible", "process_name")
                if key in window
            }
            for window in windows[:10]
            if isinstance(window, dict)
        ]
        evidence["window_count"] = len(windows)
    if not evidence:
        return "", effect_observed
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True)[:_MAX_EVIDENCE_CHARS], effect_observed


_LABELED_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PID", re.compile(r"(?i)\bpid\s*(?:=|:|é|de|do)?\s*(\d{1,7})\b")),
    ("PID", re.compile(r"(?i)\bprocesso[s]?\s+(?:n[ºo°]?\s*)?(\d{2,7})\b")),
    ("SESSION_ID", re.compile(r"(?i)\bsession\s*id\s*(?:=|:|de|do)?\s*(\d{1,5})\b")),
    ("HasExited", re.compile(r"(?i)\bhasexited\s*(?:=|:)?\s*(true|false)\b")),
    ("PORTA", re.compile(r"(?i)\bporta\s+(?:local\s+)?(\d{1,5})\b")),
    ("EXIT_CODE", re.compile(r"(?i)\bexit\s*code\s*(?:=|:)?\s*(-?\d+)\b")),
    ("LATENCIA", re.compile(r"(?i)\blat[eê]ncia\b[^.\n]{0,40}?(\d+(?:[.,]\d+)?)\s*ms\b")),
    ("PERDA", re.compile(r"(?i)(\d+(?:[.,]\d+)?)\s*%\s*(?:de\s+)?(?:perda|packet\s+loss)|perda[^.\n]{0,30}?(\d+(?:[.,]\d+)?)\s*%")),
    ("CPU", re.compile(r"(?i)\bcpu\b[^.\n]{0,30}?(\d+(?:[.,]\d+)?)\s*%")),
    ("RAM", re.compile(r"(?i)\b(?:mem[oó]ria|ram)\b[^.\n]{0,30}?(\d+(?:[.,]\d+)?)\s*%")),
)

_ABSENCE_CLAIM = re.compile(r"(?i)\b(nenhuma?[a-z]*\s+(?:inst[aâ]ncia|processo|servi[çc]o|listener|conex[aã]o)|n[aã]o\s+h[aá]|n[aã]o\s+existe)\b")

_EFFECT_FIRST_PERSON = re.compile(
    r"(?i)\b(abri|iniciei|reiniciei|parei|fechei|criei|exclu[ií]|removi|instalei|desinstalei|"
    r"conectei|desconectei|liguei|desliguei|mudei|alterei|atualizei|reinicie|inicie)\b"
)
_EFFECT_PASSIVE = re.compile(
    r"(?i)\b(foi|foram|acabei de)?\s*\b(abert[oa]s?|iniciad[oa]s?|reiniciad[oa]s?|parad[oa]s?|"
    r"criad[oa]s?|removid[oa]s?|exclu[íi]d[oa]s?|atualizad[oa]s?|restaurad[oa]s?|conectad[oa]s?|"
    r"encerrad[oa]s?|finalizad[oa]s?)\b(?:\s+com\s+sucesso)?"
)


def fabricated_value_claims(draft: str, ledger: GroundingLedger) -> list[GroundingViolation]:
    """Return labeled concrete values cited in draft with no support in any observation.

    Values that may legitimately come from operator context (IPs, hostnames, ports the
    user asked about appearing inside executed commands) are present in evidence via the
    command text itself, so they do not trigger false positives here.
    """
    evidence = ledger.evidence_text()
    violations: list[GroundingViolation] = []
    seen: set[tuple[str, str]] = set()
    for label, pattern in _LABELED_CLAIMS:
        for match in pattern.finditer(draft):
            value = next((group for group in match.groups() if group), "")
            if not value or (label, value.casefold()) in seen:
                continue
            seen.add((label, value.casefold()))
            if value.casefold() in evidence:
                continue
            if ledger.truncated_outputs():
                violations.append(GroundingViolation(
                    kind="TRUNCATED_UNVERIFIABLE",
                    detail=f"{label}={value} não aparece na evidência visível e parte da saída foi truncada.",
                ))
            else:
                violations.append(GroundingViolation(
                    kind="FABRICATED_VALUE",
                    detail=f"{label}={value} não consta em nenhum resultado de ferramenta.",
                ))
    return violations


def unverified_effect_claims(draft: str, ledger: GroundingLedger) -> list[GroundingViolation]:
    """Detect completed-effect assertions while mutations remain unverified or absent."""
    if not (_EFFECT_FIRST_PERSON.search(draft) or _EFFECT_PASSIVE.search(draft)):
        return []
    negated = bool(re.search(
        r"(?i)\bn[aã]o\s+(?:foi|foram|consegui|pude|est[aá])?\s*\w{0,20}\s*"
        r"(abert|iniciad|reiniciad|parad|criad|removid|exclu[íi]d|atualizad|conectad)",
        draft,
    ))
    if negated:
        return []
    pending = ledger.pending_mutations()
    if pending:
        return [GroundingViolation(
            kind="UNVERIFIED_EFFECT",
            detail=(
                "O rascunho afirma efeito concluído, mas as mutações executadas "
                f"({len(pending)}) ainda não possuem verificação correlata."
            ),
        )]
    mutations = [item for item in ledger.observations if item.risk_level.upper() not in READ_ONLY_RISKS]
    if not mutations:
        return [GroundingViolation(
            kind="UNVERIFIED_EFFECT",
            detail="O rascunho afirma alteração concluída sem que nenhuma mutação tenha sido executada neste turno.",
        )]
    verified = [item for item in mutations if item.verification_status == VerificationStatus.VERIFIED]
    if not verified:
        return [GroundingViolation(
            kind="UNVERIFIED_EFFECT",
            detail="As mutações deste turno não foram confirmadas por observação read-only correlata; efeito não pode ser afirmado como concluído.",
        )]
    return []


def absence_claims_without_evidence(draft: str, ledger: GroundingLedger) -> list[GroundingViolation]:
    """Absence claims are only allowed when some probe actually succeeded.

    A failed probe (access denied, timeout) leaves existence unknown: the draft may not
    convert an error into "there is none" without a successful fallback observation.
    """
    if not _ABSENCE_CLAIM.search(draft):
        return []
    if any(item.success for item in ledger.observations):
        return []
    if any(item.success is False for item in ledger.observations):
        return [GroundingViolation(
            kind="ABSENCE_WITHOUT_EVIDENCE",
            detail="A consulta falhou (erro/acesso negado/timeout); ausência não pode ser afirmada sem um fallback bem-sucedido.",
        )]
    return []


def claims_completed_effect(draft: str) -> bool:
    return bool(_EFFECT_FIRST_PERSON.search(draft) or _EFFECT_PASSIVE.search(draft))


_SUBJECT_STOPWORDS = {
    "start", "process", "processo", "powershell", "powershell.exe", "cmd", "cmd.exe",
    "system_shell", "remote_shell", "command", "comando", "timeout_seconds",
    "working_directory", "approval_id", "reason", "shell", "true", "false", "null",
}


def subject_tokens(command: str) -> set[str]:
    """Distinctive tokens of a mutation command used to correlate its verification."""
    tokens: set[str] = set()
    lowered = (command or "").casefold()
    for raw in re.findall(r"[a-z0-9_][a-z0-9_.\-]{2,}", lowered):
        token = raw.strip("-_.")
        if len(token) < 3 or token in _SUBJECT_STOPWORDS:
            continue
        if token.endswith(".exe"):
            tokens.add(token[:-4])
        tokens.add(token)
    quoted = re.findall(r"[\"']([^\"']{3,})[\"']", lowered)
    for value in quoted:
        for part in value.split():
            if part not in _SUBJECT_STOPWORDS:
                tokens.add(part)
    return tokens


def _shares_subject(mutation_command: str, verifier_command: str) -> bool:
    mutation_tokens = subject_tokens(mutation_command)
    if not mutation_tokens:
        return False
    verifier = (verifier_command or "").casefold()
    return any(token in verifier for token in mutation_tokens)


def _clean_text(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


# Backwards-compatible alias for the conceptual name in docs/spec.
ToolObservationLedger = GroundingLedger
