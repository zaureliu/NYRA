from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Awaitable, Callable

from app.selfdev.models import PromotionRecord, SelfDevRisk, SelfDevSettings
from app.selfdev.storage import ResourceLock, atomic_write_json, load_json
from app.selfdev.workspace import ShellService


def _cmd(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments)


class PromotionManager:
    def __init__(self, settings: SelfDevSettings, shell: ShellService) -> None:
        self.settings = settings
        self.shell = shell
        self.lock = ResourceLock(settings.workspace / "locks" / "promotion.lock")

    async def promote(self, issue_id: str, candidate_commit: str, risk: SelfDevRisk, *, approval_id: str | None = None) -> tuple[PromotionRecord | None, dict[str, Any]]:
        if risk == SelfDevRisk.HIGH:
            return None, {"success": False, "error_code": "HIGH_RISK_AUTOPROMOTION_BLOCKED"}
        status = await self.shell.execute(
            _cmd(["git", "status", "--porcelain"]), shell="cmd", timeout_seconds=20,
            working_directory=str(self.settings.canonical_root), reason="SelfDev promotion stable preflight",
        )
        if not status.get("success") or str(status.get("stdout") or "").strip():
            return None, {"success": False, "error_code": "PROMOTION_BLOCKED_DIRTY_STABLE"}
        with self.lock.held():
            result = await self.shell.execute(
                _cmd(["git", "cherry-pick", candidate_commit]), shell="cmd", timeout_seconds=180,
                working_directory=str(self.settings.canonical_root), approval_id=approval_id,
                reason=f"SelfDev promote validated candidate {issue_id}",
            )
        if not result.get("success"):
            return None, result
        head = await self.shell.execute(
            _cmd(["git", "rev-parse", "HEAD"]), shell="cmd", timeout_seconds=20,
            working_directory=str(self.settings.canonical_root), reason="SelfDev promotion commit readback",
        )
        record = PromotionRecord(
            issue_id=issue_id,
            candidate_commit=candidate_commit,
            promotion_commit=str(head.get("stdout") or "").strip() or None,
        )
        return record, result


class RollbackManager:
    def __init__(self, settings: SelfDevSettings, shell: ShellService) -> None:
        self.settings = settings
        self.shell = shell
        self.lock = ResourceLock(settings.workspace / "locks" / "promotion.lock")

    async def rollback(self, record: PromotionRecord, *, approval_id: str | None = None) -> dict[str, Any]:
        if not record.promotion_commit:
            return {"success": False, "error_code": "ROLLBACK_COMMIT_MISSING"}
        with self.lock.held():
            result = await self.shell.execute(
                _cmd(["git", "revert", "--no-edit", record.promotion_commit]),
                shell="cmd", timeout_seconds=180,
                working_directory=str(self.settings.canonical_root), approval_id=approval_id,
                reason=f"SelfDev rollback {record.issue_id}",
            )
        record.rollback_status = "APPLIED" if result.get("success") else "FAILED"
        return result


class RestartValidator:
    def __init__(self, pending_path: Path) -> None:
        self.pending_path = pending_path

    def prepare(self, record: PromotionRecord) -> None:
        atomic_write_json(self.pending_path, record.model_dump(mode="json"))

    def pending(self) -> PromotionRecord | None:
        value = load_json(self.pending_path, None)
        if not value:
            return None
        try:
            return PromotionRecord.model_validate(value)
        except (TypeError, ValueError):
            return None

    def complete(self, record: PromotionRecord, passed: bool) -> None:
        record.post_validation = "PASS" if passed else "FAIL"
        if passed:
            self.pending_path.unlink(missing_ok=True)
        else:
            atomic_write_json(self.pending_path, record.model_dump(mode="json"))

    async def wait_safe_idle(self, predicate: Callable[[], bool | Awaitable[bool]], *, timeout_seconds: float = 300, poll_seconds: float = 2) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return True
            await asyncio.sleep(poll_seconds)
        return False
