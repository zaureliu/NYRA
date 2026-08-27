from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any

from app.events import Event, EventType
from app.selfdev.models import (
    Evidence,
    ImprovementIssue,
    IssueStatus,
    IssueType,
    SelfDevPlan,
    SelfDevRisk,
    TaskComplexity,
)
from app.selfdev.repository import RepositoryQueryEngine
from app.selfdev.storage import atomic_write_json, load_json


class ImprovementQueue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._issues: dict[str, ImprovementIssue] = {}
        self.load()

    def load(self) -> None:
        raw = load_json(self.path, {"issues": []})
        issues = raw.get("issues", []) if isinstance(raw, dict) else []
        self._issues = {}
        for item in issues:
            try:
                issue = ImprovementIssue.model_validate(item)
                self._issues[issue.issue_id] = issue
            except (TypeError, ValueError):
                continue

    def persist(self) -> None:
        ordered = sorted(self._issues.values(), key=lambda item: item.last_seen, reverse=True)
        atomic_write_json(self.path, {"issues": [item.model_dump(mode="json") for item in ordered]})

    def upsert(self, issue: ImprovementIssue) -> ImprovementIssue:
        if issue.fingerprint:
            existing = next((item for item in self._issues.values() if item.fingerprint == issue.fingerprint), None)
            if existing:
                existing.occurrences += max(1, issue.occurrences)
                existing.last_seen = issue.last_seen
                existing.evidence = (existing.evidence + issue.evidence)[-100:]
                if existing.status == IssueStatus.EVIDENCE_GATHERING and existing.occurrences >= 3:
                    existing.status = IssueStatus.READY_FOR_PLANNING
                self.persist()
                return existing
        self._issues[issue.issue_id] = issue
        self.persist()
        return issue

    def get(self, issue_id: str) -> ImprovementIssue | None:
        return self._issues.get(issue_id)

    def list(self, *, status: IssueStatus | None = None) -> list[ImprovementIssue]:
        values = self._issues.values()
        if status is not None:
            values = (item for item in values if item.status == status)
        return sorted(values, key=lambda item: (item.priority, item.last_seen), reverse=True)

    def next_ready(self, now: datetime | None = None) -> ImprovementIssue | None:
        current = now or datetime.now(timezone.utc)
        return next((item for item in self.list(status=IssueStatus.READY_FOR_PLANNING) if not item.cooldown_until or item.cooldown_until <= current), None)

    def transition(self, issue_id: str, status: IssueStatus, *, reason: str = "") -> ImprovementIssue:
        issue = self._issues[issue_id]
        issue.status = status
        issue.last_seen = datetime.now(timezone.utc)
        if reason:
            issue.failure_reasons = [*issue.failure_reasons, reason[:500]][-20:]
        self.persist()
        return issue


class ImprovementDetector:
    FAILURE_EVENTS: dict[EventType, tuple[IssueType, str]] = {
        EventType.ERROR: (IssueType.RELIABILITY, "Erro não tratado recorrente"),
        EventType.RUNTIME_HEALTH_FAILED: (IssueType.RELIABILITY, "Falha recorrente de health do runtime"),
        EventType.RUNTIME_CRASH_LOOP: (IssueType.RELIABILITY, "Crash loop do runtime"),
        EventType.COMPUTER_VERIFICATION_FAILURE: (IssueType.BUG, "Falha recorrente de verificação de efeito"),
        EventType.COMPUTER_OPERATOR_FAILURE: (IssueType.BUG, "Falha recorrente do operador local"),
        EventType.TTS_FAILED: (IssueType.RELIABILITY, "Falha recorrente de TTS"),
        EventType.REMOTE_SHELL_APPROVAL_REQUIRED: (IssueType.INTEGRATION, "Fluxo remoto requer revisão operacional"),
    }

    def __init__(self, queue: ImprovementQueue, *, repeated_error_threshold: int = 3) -> None:
        self.queue = queue
        self.repeated_error_threshold = repeated_error_threshold

    async def observe_event(self, event: Event) -> ImprovementIssue | None:
        mapped = self.FAILURE_EVENTS.get(event.type)
        if not mapped:
            return None
        issue_type, title = mapped
        component = str(event.payload.get("component") or event.payload.get("service_id") or event.type.value)[:120]
        error_code = str(event.payload.get("error_code") or event.payload.get("stage") or event.type.value)[:120]
        fingerprint = hashlib.sha256(f"{issue_type}:{component}:{error_code}".encode()).hexdigest()
        evidence = Evidence(
            source="event_bus",
            metric=event.type.value,
            value=1,
            context={"component": component, "error_code": error_code},
        )
        issue = ImprovementIssue(
            type=issue_type,
            title=title,
            description=f"Sinal agregado {event.type.value} observado em {component}.",
            evidence=[evidence],
            affected_components=[component],
            fingerprint=fingerprint,
            status=IssueStatus.EVIDENCE_GATHERING,
            priority=90 if event.type == EventType.RUNTIME_CRASH_LOOP else 60,
        )
        stored = self.queue.upsert(issue)
        if event.type == EventType.RUNTIME_CRASH_LOOP or stored.occurrences >= self.repeated_error_threshold:
            stored.status = IssueStatus.READY_FOR_PLANNING
            self.queue.persist()
        return stored

    def explicit_feature_gap(self, title: str, description: str, components: list[str]) -> ImprovementIssue:
        fingerprint = hashlib.sha256(f"explicit:{title}:{','.join(components)}".encode()).hexdigest()
        return self.queue.upsert(ImprovementIssue(
            type=IssueType.FEATURE_GAP_EXPLICIT,
            title=title,
            description=description,
            evidence=[Evidence(source="operator", metric="explicit_request", value=True)],
            source="operator_explicit",
            affected_components=components,
            fingerprint=fingerprint,
            status=IssueStatus.READY_FOR_PLANNING,
            priority=50,
        ))


class SelfDevRiskClassifier:
    PROTECTED = {
        "grounding", "approval", "credential", "redaction", "uac", "destructive",
        "security", "publisher", "rollback", "audit", "shell_risk", "host_key",
    }
    MEDIUM_TYPES = {IssueType.CONCURRENCY, IssueType.INTEGRATION, IssueType.RESOURCE_LEAK}

    def classify(self, issue: ImprovementIssue, files: list[str] | None = None) -> SelfDevRisk:
        values = [*issue.affected_components, *(files or [])]
        lowered = " ".join(values).casefold()
        if any(marker in lowered for marker in self.PROTECTED):
            return SelfDevRisk.HIGH
        if issue.type == IssueType.SECURITY_HARDENING:
            return SelfDevRisk.HIGH
        if issue.type in self.MEDIUM_TYPES or len(files or []) > 8:
            return SelfDevRisk.MEDIUM
        return SelfDevRisk.LOW

    @staticmethod
    def can_auto_promote(risk: SelfDevRisk, mode: str) -> bool:
        return (
            risk == SelfDevRisk.LOW
            and mode in {"AUTONOMOUS_SAFE", "AUTONOMOUS_ADVANCED"}
        ) or (
            risk == SelfDevRisk.MEDIUM
            and mode == "AUTONOMOUS_ADVANCED"
        )


class SelfDevPlanner:
    def __init__(self, query: RepositoryQueryEngine, risk_classifier: SelfDevRiskClassifier) -> None:
        self.query = query
        self.risk_classifier = risk_classifier

    def create(self, issue: ImprovementIssue) -> SelfDevPlan:
        if not issue.evidence or issue.status not in {IssueStatus.READY_FOR_PLANNING, IssueStatus.PLANNING}:
            raise ValueError("ROOT_CAUSE_EVIDENCE_REQUIRED")
        related: set[str] = set()
        symbols: set[str] = set()
        for component in issue.affected_components:
            result = self.query.query(component)
            related.update(result.get("consumers", []))
            related.update(item.get("path", "") for item in result.get("definitions", []))
            symbols.update(item.get("name", "") for item in result.get("definitions", []))
        related.discard("")
        files = sorted(related)[:20]
        risk = self.risk_classifier.classify(issue, files)
        complexity = TaskComplexity.SMALL if len(files) <= 3 else TaskComplexity.MEDIUM if len(files) <= 8 else TaskComplexity.LARGE
        evidence_summary = "; ".join(f"{item.metric}={item.value}" for item in issue.evidence[-5:])
        return SelfDevPlan(
            issue_id=issue.issue_id,
            root_cause_hypothesis=f"Hipótese limitada pelos sinais observados em {', '.join(issue.affected_components) or 'runtime'}: {evidence_summary}.",
            evidence=issue.evidence,
            files_expected=files,
            symbols_expected=sorted(symbols),
            test_plan=[f"Executar testes relacionados a {path}" for path in files[:8]] or ["Adicionar reprodução targeted antes da correção"],
            benchmark_plan=["Comparar métrica before/after"] if issue.type == IssueType.PERFORMANCE else [],
            rollback_plan=["Reverter somente o commit de promoção", "Reiniciar a versão anterior", "Revalidar health read-only"],
            risk=risk,
            complexity=complexity,
            acceptance_criteria=["Reprodução falha no baseline", "Candidate passa nos testes targeted", "Scans de segurança sem achados", "Stable permanece intacto antes da promoção"],
        )


def apply_cooldown(issue: ImprovementIssue, minutes: int, reason: str) -> None:
    issue.attempts += 1
    issue.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    issue.failure_reasons = [*issue.failure_reasons, reason[:500]][-20:]
