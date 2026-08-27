from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.events import Event, EventType
from app.selfdev.improvements import ImprovementDetector, ImprovementQueue, SelfDevRiskClassifier
from app.selfdev.lifecycle import PromotionManager, RollbackManager
from app.selfdev.model_router import SelfDevModelRouter
from app.selfdev.models import (
    FileChange,
    ImprovementIssue,
    IssueStatus,
    IssueType,
    PatchBundle,
    PromotionRecord,
    SelfDevMode,
    SelfDevRisk,
    SelfDevSettings,
)
from app.selfdev.publisher import GitHubPublisher
from app.selfdev.repository import RepositoryMapper, RepositoryQueryEngine
from app.selfdev.validation import BenchmarkComparator, SecurityScanner, TestSelector as SelfDevTestSelector, ValidationPipeline
from app.selfdev.workspace import CodeWorker, TestCommandRunner as SelfDevTestCommandRunner


def _settings(tmp_path: Path) -> SelfDevSettings:
    canonical = tmp_path / "canonical"
    public = tmp_path / "public"
    workspace = tmp_path / "workspace"
    canonical.mkdir()
    public.mkdir()
    workspace.mkdir()
    return SelfDevSettings(
        mode=SelfDevMode.AUTONOMOUS_SAFE,
        workspace=workspace,
        canonical_root=canonical,
        public_snapshot=public,
    )


def test_repository_mapper_is_incremental_and_answers_symbol_and_route_queries(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "backend").mkdir(parents=True)
    (repository / "frontend").mkdir()
    (repository / "backend" / "api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "class Greeter: pass\n@router.get('/api/hello')\ndef hello(): return Greeter()\n",
        encoding="utf-8",
    )
    (repository / "frontend" / "client.ts").write_text(
        "export const GreeterClient = () => fetch('/api/hello')\n",
        encoding="utf-8",
    )
    (repository / ".env").write_text("SECRET=never-index\n", encoding="utf-8")
    mapper = RepositoryMapper(repository, tmp_path / "state" / "index.json")

    first = mapper.build()
    second = mapper.build()
    query = RepositoryQueryEngine(mapper)

    assert first.files == 2 and first.changed == 2
    assert second.reused == 2 and second.changed == 0
    assert ".env" not in mapper.index["files"]
    assert query.definitions("Greeter")[0]["path"] == "backend/api.py"
    assert query.route_consumers("/api/hello") == ["backend/api.py", "frontend/client.ts"]


@pytest.mark.asyncio
async def test_detector_requires_repeated_evidence_and_persists_deduplicated_issue(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    queue = ImprovementQueue(path)
    detector = ImprovementDetector(queue, repeated_error_threshold=3)
    event = Event(type=EventType.ERROR, payload={"component": "tts", "error_code": "TIMEOUT"})

    first = await detector.observe_event(event)
    await detector.observe_event(event)
    third = await detector.observe_event(event)

    assert first is not None and first.issue_id == third.issue_id
    assert third.occurrences == 3
    assert third.status == IssueStatus.READY_FOR_PLANNING
    restored = ImprovementQueue(path).get(third.issue_id)
    assert restored is not None and restored.occurrences == 3


def test_risk_classifier_protects_approval_security_and_shell_paths() -> None:
    issue = ImprovementIssue(
        type=IssueType.BUG,
        title="Corrigir validação",
        description="Evidência suficiente para corrigir validação.",
        affected_components=["backend/app/tools/approval.py"],
        status=IssueStatus.READY_FOR_PLANNING,
    )
    classifier = SelfDevRiskClassifier()

    assert classifier.classify(issue) == SelfDevRisk.HIGH
    assert not classifier.can_auto_promote(SelfDevRisk.HIGH, SelfDevMode.AUTONOMOUS_ADVANCED.value)


def test_code_worker_enforces_hash_containment_and_secret_rejection(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    candidate = settings.workspace / "worktrees" / "SELFDEV-ABCD"
    target = candidate / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    worker = CodeWorker(settings)
    good = PatchBundle(
        issue_id="SELFDEV-ABCD",
        rationale="Atualização validada",
        changes=[FileChange(path="backend/app.py", operation="UPDATE", content="VALUE = 2\n", expected_sha256=digest)],
    )

    assert worker.apply(candidate, good) == ["backend/app.py"]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    with pytest.raises(ValueError):
        FileChange(path="../escape.py", operation="CREATE", content="pass\n")
    secret = PatchBundle(
        issue_id="SELFDEV-ABCD",
        rationale="deve falhar",
        changes=[FileChange(path="backend/leak.py", operation="CREATE", content="api_key = 'sk-abcdefghijklmnopqrstuvwxyz'\n")],
    )
    with pytest.raises(PermissionError, match="SECRET"):
        worker.apply(candidate, secret)

    settings.max_diff_lines_low_risk = 2
    oversized = PatchBundle(
        issue_id="SELFDEV-ABCD",
        rationale="deve exceder limite",
        changes=[FileChange(path="backend/large.py", operation="CREATE", content="a = 1\nb = 2\nc = 3\n")],
    )
    with pytest.raises(ValueError, match="DIFF_LIMIT"):
        worker.apply(candidate, oversized)


class _Runner:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.commands: list[str] = []

    async def run(self, command: str, cwd: Path, timeout_seconds: int, reason: str) -> dict:
        self.commands.append(command)
        return {"success": self.success, "stdout": "ok" if self.success else "failed"}


@pytest.mark.asyncio
async def test_validation_blocks_secrets_before_commands_and_accepts_clean_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    (candidate / "backend").mkdir(parents=True)
    changed = candidate / "backend" / "clean.py"
    changed.write_text("VALUE = 1\n", encoding="utf-8")
    runner = _Runner()
    pipeline = ValidationPipeline(runner, SelfDevTestSelector(), SecurityScanner())

    passed = await pipeline.validate("SELFDEV-TEST", candidate, ["backend/clean.py"])
    assert passed.passed
    assert runner.commands

    changed.write_text("token = 'sk-abcdefghijklmnopqrstuvwxyz'\n", encoding="utf-8")
    runner.commands.clear()
    blocked = await pipeline.validate("SELFDEV-TEST", candidate, ["backend/clean.py"])
    assert not blocked.passed and blocked.security_findings
    assert runner.commands == []


@pytest.mark.asyncio
async def test_test_command_runner_rejects_arbitrary_shell() -> None:
    class Shell:
        async def execute(self, *args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("unexpected execution")

    runner = SelfDevTestCommandRunner(Shell())
    with pytest.raises(PermissionError, match="NOT_ALLOWLISTED"):
        await runner.run("powershell Remove-Item x", Path.cwd(), 1, "invalid")


def test_benchmark_comparator_reports_improvement() -> None:
    result = BenchmarkComparator().compare("latency_ms", [100, 110], [70, 80])
    assert result.improved
    assert result.before == 105
    assert result.after == 75


class _Shell:
    def __init__(self, *, dirty: bool = False) -> None:
        self.dirty = dirty
        self.commands: list[str] = []

    async def execute(self, command: str, **kwargs) -> dict:
        self.commands.append(command)
        if command.startswith("git status"):
            return {"success": True, "stdout": " M existing.py" if self.dirty else ""}
        if command.startswith("git rev-parse"):
            return {"success": True, "stdout": "promoted123\n"}
        return {"success": True, "stdout": "ok"}


@pytest.mark.asyncio
async def test_promotion_blocks_dirty_stable_and_rollback_uses_revert(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    dirty_shell = _Shell(dirty=True)
    record, result = await PromotionManager(settings, dirty_shell).promote("SELFDEV-ABCD", "candidate123", SelfDevRisk.LOW)
    assert record is None and result["error_code"] == "PROMOTION_BLOCKED_DIRTY_STABLE"

    clean_shell = _Shell()
    record, result = await PromotionManager(settings, clean_shell).promote("SELFDEV-ABCD", "candidate123", SelfDevRisk.LOW)
    assert result["success"] and record is not None and record.promotion_commit == "promoted123"
    rollback = await RollbackManager(settings, clean_shell).rollback(record)
    assert rollback["success"]
    assert any(command.startswith("git revert --no-edit") for command in clean_shell.commands)
    assert all("reset" not in command and "clean" not in command for command in clean_shell.commands)


@pytest.mark.asyncio
async def test_publisher_blocks_dirty_snapshot_before_copying(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.auto_publish_github = True
    (settings.public_snapshot / ".git").mkdir()
    (settings.canonical_root / "README.md").write_text("# release\n", encoding="utf-8")
    shell = _Shell(dirty=True)
    publisher = GitHubPublisher(settings, shell, SecurityScanner())

    result = await publisher.publish("SELFDEV-ABCD", "release segura", SelfDevRisk.LOW)

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "PUBLIC_SNAPSHOT_DIRTY"
    assert not (settings.public_snapshot / "README.md").exists()


@pytest.mark.asyncio
async def test_model_router_rejects_non_loopback_before_inventory() -> None:
    class Brain:
        async def inventory(self):  # pragma: no cover - must not be reached
            raise AssertionError("external inventory must not be queried")

    router = SelfDevModelRouter(Brain(), base_url="https://models.example.test", model="qwen3:8b")
    with pytest.raises(PermissionError, match="LOCAL_MODEL_REQUIRED"):
        await router.installed_models()
