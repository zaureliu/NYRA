from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Awaitable, Callable

from app.core.paths import PROJECT_ROOT, RUNTIME_ROOT
from app.events import Event, EventBus, EventType
from app.selfdev.improvements import ImprovementDetector, ImprovementQueue, SelfDevPlanner, SelfDevRiskClassifier, apply_cooldown
from app.selfdev.lifecycle import PromotionManager, RestartValidator, RollbackManager
from app.selfdev.model_router import SelfDevModelRouter
from app.selfdev.models import (
    ImprovementIssue,
    IssueStatus,
    NotificationType,
    PatchBundle,
    PromotionRecord,
    SelfDevMode,
    SelfDevRisk,
    SelfDevSettings,
    SelfDevStatus,
)
from app.selfdev.notifications import SelfDevDocumentation, SelfDevNotificationCenter
from app.selfdev.observer import RuntimeObserver
from app.selfdev.publisher import GitHubPublisher
from app.selfdev.repository import RepositoryMapper, RepositoryQueryEngine
from app.selfdev.scheduler import SelfDevScheduler
from app.selfdev.storage import ResourceLockError, atomic_write_json, load_json
from app.selfdev.validation import BenchmarkComparator, RegressionDetector, SecurityScanner, TestSelector, ValidationPipeline
from app.selfdev.workspace import CodeWorker, TestCommandRunner, WorktreeManager


RestartRequest = Callable[[str], Awaitable[dict[str, Any]]]
HealthCheck = Callable[[], bool | Awaitable[bool]]


def _preferred_path(env_name: str, preferred: Path, fallback: Path) -> Path:
    override = os.environ.get(env_name)
    if override:
        return Path(override)
    return preferred if preferred.parent.exists() else fallback


def _cmd(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments)


class SelfDevelopmentService:
    """Fault-isolated coordinator for evidence -> candidate -> validation -> promotion."""

    def __init__(
        self,
        runtime_settings: Any,
        event_bus: EventBus,
        shell: Any,
        brain: Any,
        *,
        restart_request: RestartRequest | None = None,
        health_check: HealthCheck | None = None,
        scheduler_interval_seconds: float = 30,
    ) -> None:
        self.runtime_settings = runtime_settings
        self.event_bus = event_bus
        self.shell = shell
        self.brain = brain
        self.restart_request = restart_request
        self.health_check = health_check or (lambda: True)
        self.settings = self._settings_from_runtime()
        self.state_root = RUNTIME_ROOT / "selfdev"
        self.mapper = RepositoryMapper(self.settings.canonical_root, self.state_root / "repo-index" / "index.json")
        self.query_engine = RepositoryQueryEngine(self.mapper)
        self.observer = RuntimeObserver(event_bus, self.state_root / "runtime-metrics.json")
        self.queue = ImprovementQueue(self.state_root / "queue.json")
        self.detector = ImprovementDetector(self.queue)
        self.risk = SelfDevRiskClassifier()
        self.planner = SelfDevPlanner(self.query_engine, self.risk)
        self.worktrees = WorktreeManager(self.settings, shell)
        self.worker = CodeWorker(self.settings)
        self.runner = TestCommandRunner(shell)
        self.scanner = SecurityScanner()
        self.selector = TestSelector(self.query_engine)
        self.validation = ValidationPipeline(self.runner, self.selector, self.scanner)
        self.regression = RegressionDetector()
        self.benchmarks = BenchmarkComparator()
        self.promotion = PromotionManager(self.settings, shell)
        self.rollback = RollbackManager(self.settings, shell)
        self.restart = RestartValidator(self.state_root / "pending-restart.json")
        self.notifications = SelfDevNotificationCenter(self.state_root / "notifications.json")
        self.documentation = SelfDevDocumentation(self.state_root / "reports")
        self.model_router = SelfDevModelRouter(
            brain,
            base_url=str(runtime_settings.ollama_url),
            model=self.settings.model,
            timeout=float(runtime_settings.llm_timeout_seconds),
            context_size=int(runtime_settings.ollama_context_size),
            keep_alive=str(runtime_settings.ollama_keep_alive),
        )
        self.publisher = GitHubPublisher(self.settings, shell, self.scanner)
        self.scheduler = SelfDevScheduler(
            self.settings, self.run_once, self.safe_idle, interval_seconds=scheduler_interval_seconds
        )
        self._state = "STARTING"
        self._last_error_code: str | None = None
        self._active_issue_id: str | None = None
        self._last_activity = time.monotonic()
        self._voice_active = False
        self._run_lock = asyncio.Lock()
        self._started = False

    def _settings_from_runtime(self) -> SelfDevSettings:
        workspace = Path(getattr(self.runtime_settings, "selfdev_workspace", "") or _preferred_path(
            "NYRA_SELFDEV_WORKSPACE", PROJECT_ROOT.parent / "Nyra-Auto-Code", RUNTIME_ROOT / "selfdev-workspace"
        ))
        public = Path(getattr(self.runtime_settings, "selfdev_public_snapshot", "") or _preferred_path(
            "NYRA_PUBLIC_SNAPSHOT", PROJECT_ROOT.parent / "NYRA-GitHub-Public", PROJECT_ROOT.parent / "NYRA-GitHub-Public"
        ))
        canonical = Path(getattr(self.runtime_settings, "selfdev_canonical_root", "") or PROJECT_ROOT)
        return SelfDevSettings(
            mode=SelfDevMode(str(getattr(self.runtime_settings, "selfdev_mode", "AUTONOMOUS_SAFE"))),
            model=str(getattr(self.runtime_settings, "selfdev_model", "qwen3:8b")),
            workspace=workspace,
            canonical_root=canonical,
            public_snapshot=public,
            run_when_idle=bool(getattr(self.runtime_settings, "selfdev_run_when_idle", True)),
            auto_publish_github=bool(getattr(self.runtime_settings, "selfdev_auto_publish_github", False)),
            max_auto_promotions_per_day=int(getattr(self.runtime_settings, "selfdev_max_auto_promotions_per_day", 3)),
            max_candidate_runtime_minutes=int(getattr(self.runtime_settings, "selfdev_max_candidate_runtime_minutes", 30)),
            max_files_low_risk=int(getattr(self.runtime_settings, "selfdev_max_files_low_risk", 8)),
            max_diff_lines_low_risk=int(getattr(self.runtime_settings, "selfdev_max_diff_lines_low_risk", 500)),
            cooldown_minutes=int(getattr(self.runtime_settings, "selfdev_cooldown_minutes", 15)),
        )

    async def start(self) -> None:
        if self._started:
            return
        if self.settings.mode == SelfDevMode.OFF:
            self._state = "OFF"
            self._started = True
            return
        try:
            self._ensure_workspace()
            stats = await asyncio.to_thread(self.mapper.build)
            await self.observer.start()
            await self.event_bus.subscribe(self._observe_event)
            await self.event_bus.subscribe(self.detector.observe_event)
            self.scheduler.start()
            await self._reconcile_pending_restart()
            self._state = "READY"
            self._last_error_code = None
            self._started = True
            atomic_write_json(self.settings.workspace / "state" / "workspace.json", {
                "version": 1,
                "canonical_root": str(self.settings.canonical_root),
                "public_snapshot": str(self.settings.public_snapshot),
                "repository_files": stats.files,
                "auto_publish_github": self.settings.auto_publish_github,
            })
        except Exception as error:
            self._state = "DEGRADED"
            self._last_error_code = type(error).__name__
            self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self.scheduler.stop()
        await self.observer.stop()
        await self.event_bus.unsubscribe(self._observe_event)
        await self.event_bus.unsubscribe(self.detector.observe_event)
        self._started = False

    async def refresh_settings(self) -> None:
        previous_mode = self.settings.mode
        fresh = self._settings_from_runtime()
        self.settings.mode = fresh.mode
        self.settings.model = fresh.model
        self.settings.run_when_idle = fresh.run_when_idle
        self.settings.auto_publish_github = fresh.auto_publish_github
        self.settings.max_auto_promotions_per_day = fresh.max_auto_promotions_per_day
        self.settings.max_candidate_runtime_minutes = fresh.max_candidate_runtime_minutes
        self.settings.max_files_low_risk = fresh.max_files_low_risk
        self.settings.max_diff_lines_low_risk = fresh.max_diff_lines_low_risk
        self.settings.cooldown_minutes = fresh.cooldown_minutes
        self.model_router.model = fresh.model
        if previous_mode != SelfDevMode.OFF and fresh.mode == SelfDevMode.OFF:
            await self.stop()
            self._state = "OFF"
            self._started = True
        elif previous_mode == SelfDevMode.OFF and fresh.mode != SelfDevMode.OFF:
            self._started = False
            await self.start()
        elif fresh.mode != SelfDevMode.OFF:
            self._state = "READY" if not self._run_lock.locked() else "BUSY"

    def status(self) -> dict[str, Any]:
        result = SelfDevStatus(
            state=self._state,
            mode=self.settings.mode,
            active_issue_id=self._active_issue_id,
            queue_size=len([item for item in self.queue.list() if item.status not in {IssueStatus.APPLIED, IssueStatus.REJECTED, IssueStatus.ROLLED_BACK}]),
            unread_notifications=self.notifications.unread_count(),
            repository_files=len(self.mapper.index.get("files", {})),
            workspace_ready=self.settings.workspace.is_dir(),
            github_status="ON" if self.settings.auto_publish_github else "OFF",
            last_error_code=self._last_error_code,
        )
        return result.model_dump(mode="json")

    async def safe_idle(self) -> bool:
        if self._voice_active or time.monotonic() - self._last_activity < 30:
            return False
        try:
            import psutil

            if psutil.cpu_percent(interval=None) > self.settings.max_cpu_percent:
                return False
            if psutil.virtual_memory().available < self.settings.min_free_ram_gb * 1024**3:
                return False
        except (ImportError, OSError):
            pass
        return True

    async def _observe_event(self, event: Event) -> None:
        active = {
            EventType.USER_SPEECH_STARTED, EventType.USER_TEXT_RECEIVED,
            EventType.LLM_PROCESSING, EventType.TTS_STARTED, EventType.PLAYBACK_STARTED,
        }
        inactive = {
            EventType.USER_SPEECH_FINAL, EventType.TTS_FINISHED,
            EventType.TTS_FAILED, EventType.SPEECH_CANCELLED,
        }
        if event.type in active:
            self._last_activity = time.monotonic()
        if event.type == EventType.USER_SPEECH_STARTED:
            self._voice_active = True
        elif event.type in inactive:
            self._voice_active = False

    def submit_explicit_issue(self, title: str, description: str, components: list[str]) -> dict[str, Any]:
        issue = self.detector.explicit_feature_gap(title, description, components)
        self.notifications.add(NotificationType.ISSUE_DETECTED, issue.title, issue.description, issue_id=issue.issue_id)
        self._schedule_audit(
            EventType.SELFDEV_ISSUE_DETECTED,
            issue_id=issue.issue_id,
            issue_type=issue.type.value,
            title=issue.title,
        )
        return issue.model_dump(mode="json")

    def issues(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.queue.list()]

    def notification_items(self, *, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.notifications.list(unread_only=unread_only, limit=limit)]

    def mark_notification_read(self, notification_id: str) -> bool:
        return self.notifications.mark_read(notification_id)

    async def installed_models(self) -> list[str]:
        return await self.model_router.installed_models()

    async def run_once(self, *, issue_id: str | None = None, bundle: PatchBundle | None = None, bypass_idle: bool = False) -> dict[str, Any]:
        if self.settings.mode in {SelfDevMode.OFF, SelfDevMode.OBSERVE_ONLY}:
            return {"status": "BLOCKED", "error_code": "SELFDEV_MODE_OBSERVE_ONLY"}
        if self._run_lock.locked():
            return {"status": "BLOCKED", "error_code": "SELFDEV_BUSY"}
        if not bypass_idle and not await self.safe_idle():
            return {"status": "BLOCKED", "error_code": "SAFE_IDLE_REQUIRED"}
        async with self._run_lock:
            issue = self.queue.get(issue_id) if issue_id else self.queue.next_ready()
            if issue is None:
                return {"status": "IDLE", "error_code": "NO_READY_ISSUE"}
            try:
                return await asyncio.wait_for(
                    self._run_issue(issue, bundle=bundle),
                    timeout=self.settings.max_candidate_runtime_minutes * 60,
                )
            except TimeoutError:
                apply_cooldown(issue, self.settings.cooldown_minutes, "CANDIDATE_TIMEOUT")
                issue.status = IssueStatus.BLOCKED
                self.queue.persist()
                self.notifications.add(
                    NotificationType.BLOCKED,
                    issue.title,
                    "Candidate excedeu o limite total de execução.",
                    issue_id=issue.issue_id,
                )
                return {"status": "BLOCKED", "error_code": "CANDIDATE_TIMEOUT"}

    async def _run_issue(self, issue: ImprovementIssue, *, bundle: PatchBundle | None) -> dict[str, Any]:
        self._state = "BUSY"
        self._active_issue_id = issue.issue_id
        candidate: Path | None = None
        try:
            self.queue.transition(issue.issue_id, IssueStatus.PLANNING)
            plan = self.planner.create(issue)
            await self._audit(EventType.SELFDEV_PLAN_CREATED, issue_id=issue.issue_id, risk=plan.risk.value)
            issue.risk = plan.risk
            self.queue.persist()
            if not self.risk.can_auto_promote(plan.risk, self.settings.mode.value):
                self.queue.transition(issue.issue_id, IssueStatus.BLOCKED, reason="RISK_NOT_AUTOPROMOTABLE")
                self.notifications.add(NotificationType.BLOCKED, issue.title, f"Risco {plan.risk} requer revisão.", issue_id=issue.issue_id)
                return {"status": "BLOCKED", "error_code": "RISK_NOT_AUTOPROMOTABLE", "plan": plan.model_dump(mode="json")}
            planned_files = [change.path for change in bundle.changes] if bundle is not None else plan.files_expected
            reproduction = self.validation.reproduce(issue, plan)
            if reproduction.status != "PASS":
                self.queue.transition(issue.issue_id, IssueStatus.BLOCKED, reason="REPRODUCTION_EVIDENCE_REQUIRED")
                return {"status": "BLOCKED", "error_code": "REPRODUCTION_EVIDENCE_REQUIRED",
                        "reproduction": reproduction.model_dump(mode="json")}
            # Baseline executes only in the isolated SelfDev repository mirror.
            # The operational runtime is never used as an experimental workspace.
            baseline_root = self.settings.workspace / "repository"
            if not (baseline_root / "backend").is_dir():
                self.queue.transition(issue.issue_id, IssueStatus.BLOCKED, reason="SELFDEV_BASELINE_MIRROR_MISSING")
                return {"status": "BLOCKED", "error_code": "SELFDEV_BASELINE_MIRROR_MISSING"}
            baseline = await self.validation.capture_baseline(
                issue.issue_id, baseline_root, planned_files,
            )
            candidate = await self.worktrees.create(issue.issue_id, issue.title)
            await self._audit(EventType.SELFDEV_WORKTREE_CREATED, issue_id=issue.issue_id)
            self.queue.transition(issue.issue_id, IssueStatus.IMPLEMENTING)
            if bundle is None:
                context = self._repository_context(plan.files_expected)
                bundle = await self.model_router.propose_patch(plan, context)
            if bundle.issue_id != issue.issue_id:
                raise ValueError("PATCH_ISSUE_MISMATCH")
            changed = self.worker.apply(candidate, bundle)
            await self._audit(EventType.SELFDEV_PATCH_READY, issue_id=issue.issue_id, changed_files=len(changed))
            self.queue.transition(issue.issue_id, IssueStatus.VALIDATING)
            report = await self.validation.validate(
                issue.issue_id, candidate, changed, plan=plan,
                baseline=baseline, reproduction=reproduction,
            )
            issue.last_validation = "PASS" if report.passed else "FAIL"
            self.queue.persist()
            if not report.passed:
                await self._audit(EventType.SELFDEV_VALIDATION_FAIL, issue_id=issue.issue_id)
                self.queue.transition(issue.issue_id, IssueStatus.REJECTED, reason="CANDIDATE_VALIDATION_FAILED")
                return {"status": "REJECTED", "validation": report.model_dump(mode="json")}
            await self._audit(EventType.SELFDEV_VALIDATION_PASS, issue_id=issue.issue_id)
            candidate_commit = await self._commit_candidate(issue, candidate, changed)
            if not candidate_commit.get("success"):
                self.queue.transition(issue.issue_id, IssueStatus.BLOCKED, reason=str(candidate_commit.get("error_code") or "CANDIDATE_COMMIT_FAILED"))
                return {"status": "BLOCKED", "commit": candidate_commit}
            commit_hash = str(candidate_commit.get("commit") or "")
            issue.last_candidate = commit_hash
            self.queue.transition(issue.issue_id, IssueStatus.READY_TO_PROMOTE)
            record, promotion = await self.promotion.promote(issue.issue_id, commit_hash, issue.risk)
            if record is None:
                self.queue.transition(issue.issue_id, IssueStatus.BLOCKED, reason=str(promotion.get("error_code") or "PROMOTION_FAILED"))
                return {"status": "BLOCKED", "promotion": promotion}
            self._save_promotion(record)
            await self._audit(EventType.SELFDEV_PROMOTION_APPLIED, issue_id=issue.issue_id)
            self.restart.prepare(record)
            self.scheduler.note_promotion()
            self.queue.transition(issue.issue_id, IssueStatus.POST_VALIDATING)
            self.notifications.add(
                NotificationType.IMPROVEMENT_PREPARED,
                issue.title,
                "Candidate promovido; restart e pós-validação estão pendentes.",
                issue_id=issue.issue_id,
                details={"files": changed, "candidate_commit": commit_hash, "promotion_commit": record.promotion_commit},
            )
            if self.restart_request:
                await self.restart_request(f"selfdev:{issue.issue_id}")
            return {"status": "POST_VALIDATING", "promotion": record.model_dump(mode="json")}
        except (ValueError, RuntimeError, PermissionError, FileExistsError, ResourceLockError) as error:
            apply_cooldown(issue, self.settings.cooldown_minutes, type(error).__name__)
            issue.status = IssueStatus.BLOCKED
            self.queue.persist()
            self._last_error_code = str(error)[:120]
            self.notifications.add(NotificationType.BLOCKED, issue.title, self._last_error_code, issue_id=issue.issue_id)
            return {"status": "BLOCKED", "error_code": self._last_error_code, "candidate": str(candidate) if candidate else None}
        finally:
            self._active_issue_id = None
            self._state = "READY" if self.settings.mode != SelfDevMode.OFF else "OFF"

    async def _commit_candidate(self, issue: ImprovementIssue, candidate: Path, changed: list[str]) -> dict[str, Any]:
        add = await self.shell.execute(
            _cmd(["git", "add", "--", *changed]), shell="cmd", timeout_seconds=60,
            working_directory=str(candidate), reason=f"SelfDev stage validated candidate {issue.issue_id}",
        )
        if not add.get("success"):
            return add
        commit = await self.shell.execute(
            _cmd(["git", "commit", "-m", f"selfdev({issue.issue_id}): {issue.title[:72]}"]),
            shell="cmd", timeout_seconds=60, working_directory=str(candidate),
            reason=f"SelfDev commit validated candidate {issue.issue_id}",
        )
        if not commit.get("success"):
            return commit
        head = await self.shell.execute(
            _cmd(["git", "rev-parse", "HEAD"]), shell="cmd", timeout_seconds=20,
            working_directory=str(candidate), reason="SelfDev candidate commit readback",
        )
        return {**commit, "commit": str(head.get("stdout") or "").strip()}

    async def _reconcile_pending_restart(self) -> None:
        record = self.restart.pending()
        if record is None:
            return
        value = self.health_check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            self.restart.complete(record, True)
            if self.queue.get(record.issue_id) is not None:
                self.queue.transition(record.issue_id, IssueStatus.APPLIED)
            publication = await self._publish_validated(record)
            self._update_promotion(record)
            self.documentation.write_report(record.issue_id, {
                "problem": self.queue.get(record.issue_id).description if self.queue.get(record.issue_id) else "N/A",
                "promotion_commit": record.promotion_commit,
                "post_validation": "PASS",
                "rollback": "not required",
                "github": publication.get("status", "OFF"),
            })
            self.notifications.add(NotificationType.IMPROVEMENT_APPLIED, record.issue_id, "Restart e pós-validação concluídos.", issue_id=record.issue_id)
            await self._audit(EventType.SELFDEV_POST_VALIDATION_PASS, issue_id=record.issue_id)
            return
        self.restart.complete(record, False)
        result = await self.rollback.rollback(record)
        self._update_promotion(record)
        if result.get("success"):
            if self.queue.get(record.issue_id) is not None:
                self.queue.transition(record.issue_id, IssueStatus.ROLLED_BACK)
            self.notifications.add(NotificationType.ROLLBACK_OCCURRED, record.issue_id, "Pós-validação falhou; versão anterior restaurada.", issue_id=record.issue_id)
            await self._audit(EventType.SELFDEV_ROLLBACK, issue_id=record.issue_id, trigger="post_validation")
            if self.restart_request:
                await self.restart_request(f"selfdev-rollback:{record.issue_id}")

    async def _publish_validated(self, record: PromotionRecord) -> dict[str, Any]:
        issue = self.queue.get(record.issue_id)
        title = issue.title if issue else record.issue_id
        risk = issue.risk if issue else SelfDevRisk.HIGH
        result = await self.publisher.publish(record.issue_id, title, risk)
        status = str(result.get("status") or "FAILED")
        if status in {"OFF", "PENDING", "PUBLISHED", "BLOCKED", "FAILED"}:
            record.github_status = status
        if status == "PUBLISHED":
            await self._audit(EventType.SELFDEV_GITHUB_PUSHED, issue_id=record.issue_id)
        elif status in {"BLOCKED", "FAILED"}:
            kind = NotificationType.SECURITY_BLOCKED if status == "BLOCKED" else NotificationType.PUBLISH_FAILED
            self.notifications.add(kind, title, str(result.get("error_code") or status), issue_id=record.issue_id)
            await self._audit(EventType.SELFDEV_GITHUB_BLOCKED, issue_id=record.issue_id, status=status)
        return result

    async def revert(self, issue_id: str, *, approval_id: str | None = None) -> dict[str, Any]:
        record = next((item for item in self._promotion_history() if item.issue_id == issue_id), None)
        if record is None:
            return {"success": False, "error_code": "PROMOTION_NOT_FOUND"}
        result = await self.rollback.rollback(record, approval_id=approval_id)
        self._update_promotion(record)
        if result.get("success"):
            if self.queue.get(issue_id) is not None:
                self.queue.transition(issue_id, IssueStatus.ROLLED_BACK)
            self.notifications.add(NotificationType.ROLLBACK_OCCURRED, issue_id, "Rollback solicitado pelo operador e aplicado.", issue_id=issue_id)
            await self._audit(EventType.SELFDEV_ROLLBACK, issue_id=issue_id, trigger="operator")
        return result

    def issue_details(self, issue_id: str) -> dict[str, Any] | None:
        issue = self.queue.get(issue_id)
        if issue is None:
            return None
        promotions = [item.model_dump(mode="json") for item in self._promotion_history() if item.issue_id == issue_id]
        return {"issue": issue.model_dump(mode="json"), "promotions": promotions}

    def repository_query(self, question: str) -> dict[str, Any]:
        return self.query_engine.query(question)

    async def issue_diff(self, issue_id: str) -> dict[str, Any] | None:
        issue = self.queue.get(issue_id)
        if issue is None:
            return None
        commit = issue.last_candidate
        if not commit:
            return {"issue_id": issue_id, "commit": None, "diff": ""}
        result = await self.shell.execute(
            _cmd(["git", "show", "--format=", "--stat", "--patch", commit]),
            shell="cmd",
            timeout_seconds=30,
            working_directory=str(self.settings.canonical_root),
            reason=f"SelfDev read-only diff for {issue_id}",
        )
        if not result.get("success"):
            return {
                "issue_id": issue_id,
                "commit": commit,
                "diff": "",
                "error_code": result.get("error_code") or "DIFF_UNAVAILABLE",
            }
        return {
            "issue_id": issue_id,
            "commit": commit,
            "diff": str(result.get("stdout") or "")[:200_000],
        }

    async def _audit(self, event_type: EventType, **payload: str | int | float | bool | None) -> None:
        await self.event_bus.publish(event_type, **payload)

    def _schedule_audit(self, event_type: EventType, **payload: str | int | float | bool | None) -> None:
        try:
            asyncio.get_running_loop().create_task(self._audit(event_type, **payload))
        except RuntimeError:
            return

    def _repository_context(self, files: list[str]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for relative in files[:8]:
            path = self.settings.canonical_root / relative
            entry = self.mapper.index.get("files", {}).get(relative, {})
            if not path.is_file() or path.stat().st_size > 120_000:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            output[relative] = {"sha256": entry.get("sha256") or hashlib.sha256(path.read_bytes()).hexdigest(), "content": content}
        return output

    def _ensure_workspace(self) -> None:
        for name in ("repository", "worktrees", "state", "reports", "artifacts", "rejected", "locks"):
            (self.settings.workspace / name).mkdir(parents=True, exist_ok=True)
        if not self.settings.canonical_root.is_dir():
            raise FileNotFoundError("SELFDEV_CANONICAL_ROOT_MISSING")

    @property
    def _promotions_path(self) -> Path:
        return self.state_root / "promotions.json"

    def _promotion_history(self) -> list[PromotionRecord]:
        values = load_json(self._promotions_path, {"promotions": []}).get("promotions", [])
        output = []
        for value in values:
            try:
                output.append(PromotionRecord.model_validate(value))
            except (TypeError, ValueError):
                continue
        return output

    def _save_promotion(self, record: PromotionRecord) -> None:
        values = [record, *self._promotion_history()]
        atomic_write_json(self._promotions_path, {"promotions": [item.model_dump(mode="json") for item in values[:500]]})

    def _update_promotion(self, record: PromotionRecord) -> None:
        values = [record if item.issue_id == record.issue_id else item for item in self._promotion_history()]
        atomic_write_json(self._promotions_path, {"promotions": [item.model_dump(mode="json") for item in values[:500]]})
