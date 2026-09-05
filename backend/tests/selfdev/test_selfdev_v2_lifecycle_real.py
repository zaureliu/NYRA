from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from app.selfdev.lifecycle import PromotionManager, RestartValidator, RollbackManager
from app.selfdev.models import SelfDevMode, SelfDevRisk, SelfDevSettings


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


class _RealGitShell:
    """Test adapter executing only the four commands emitted by lifecycle.py."""

    _ALLOWED = re.compile(
        r"^git (?:status --porcelain|rev-parse HEAD|cherry-pick [0-9a-f]{40}|revert --no-edit [0-9a-f]{40})$"
    )

    async def execute(self, command: str, *, working_directory: str, **_kwargs) -> dict:
        if not self._ALLOWED.fullmatch(command):
            return {"success": False, "error_code": "TEST_COMMAND_NOT_ALLOWLISTED"}

        def run() -> dict:
            completed = subprocess.run(
                command.split(), cwd=working_directory, capture_output=True, text=True,
                timeout=30, check=False,
            )
            return {
                "success": completed.returncode == 0,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
            }

        return await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_low_risk_real_git_promotion_failed_health_and_rollback(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    workspace = tmp_path / "selfdev"
    public = tmp_path / "public"
    for directory in (canonical, workspace, public):
        directory.mkdir()

    _git(canonical, "init", "-b", "main")
    _git(canonical, "config", "user.name", "KAZUMI SelfDev Test")
    _git(canonical, "config", "user.email", "selfdev-test@invalid.local")
    target = canonical / "bounded.txt"
    target.write_text("stable\n", encoding="utf-8")
    _git(canonical, "add", "bounded.txt")
    _git(canonical, "commit", "-m", "baseline")
    _git(canonical, "worktree", "add", "-b", "candidate", str(candidate))
    (candidate / "bounded.txt").write_text("candidate intentionally fails health\n", encoding="utf-8")
    _git(candidate, "add", "bounded.txt")
    _git(candidate, "commit", "-m", "low risk candidate")
    candidate_commit = _git(candidate, "rev-parse", "HEAD")

    settings = SelfDevSettings(
        mode=SelfDevMode.AUTONOMOUS_SAFE,
        workspace=workspace,
        canonical_root=canonical,
        public_snapshot=public,
    )
    shell = _RealGitShell()
    record, promotion = await PromotionManager(settings, shell).promote(
        "SELFDEV-REAL-ROLLBACK", candidate_commit, SelfDevRisk.LOW,
    )
    assert promotion["success"] and record is not None
    assert target.read_text(encoding="utf-8") == "candidate intentionally fails health\n"

    restart = RestartValidator(workspace / "state" / "pending-restart.json")
    restart.prepare(record)
    assert restart.pending() is not None
    restart.complete(record, passed=False)
    assert restart.pending() is not None and restart.pending().post_validation == "FAIL"

    rollback = await RollbackManager(settings, shell).rollback(record)
    assert rollback["success"]
    assert record.rollback_status == "APPLIED"
    assert target.read_text(encoding="utf-8") == "stable\n"
    assert "Revert" in _git(canonical, "log", "-1", "--pretty=%s")
