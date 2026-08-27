from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Protocol
from uuid import uuid4

from app.selfdev.models import PatchBundle, SelfDevSettings
from app.selfdev.storage import ResourceLock, atomic_write_json, contained_path


class ShellService(Protocol):
    async def execute(
        self,
        command: str,
        shell: str | None = None,
        timeout_seconds: int | None = None,
        working_directory: str | None = None,
        approval_id: str | None = None,
        reason: str = "",
        elevate: bool = False,
    ) -> dict[str, Any]: ...


def _cmd(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments)


class WorktreeManager:
    ISSUE_ID = re.compile(r"^SELFDEV-[A-Z0-9-]{4,64}$")

    def __init__(self, settings: SelfDevSettings, shell: ShellService) -> None:
        self.settings = settings
        self.shell = shell
        self.worktrees_root = settings.workspace / "worktrees"
        self.locks_root = settings.workspace / "locks"

    async def stable_status(self) -> dict[str, Any]:
        return await self.shell.execute(
            _cmd(["git", "status", "--porcelain"]),
            shell="cmd",
            timeout_seconds=20,
            working_directory=str(self.settings.canonical_root),
            reason="selfdev stable preflight read-only",
        )

    async def create(self, issue_id: str, title: str) -> Path:
        if not self.ISSUE_ID.fullmatch(issue_id):
            raise ValueError("invalid SelfDev issue id")
        status = await self.stable_status()
        if not status.get("success"):
            raise RuntimeError("STABLE_GIT_STATUS_FAILED")
        if str(status.get("stdout") or "").strip():
            raise RuntimeError("PROMOTION_BLOCKED_DIRTY_STABLE")
        slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:40] or "candidate"
        branch = f"selfdev/{issue_id}-{slug}"
        target = contained_path(self.worktrees_root, issue_id)
        if target.exists():
            raise FileExistsError(f"candidate already exists: {issue_id}")
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        lock = ResourceLock(self.locks_root / "repo_write.lock")
        with lock.held():
            result = await self.shell.execute(
                _cmd(["git", "worktree", "add", "-b", branch, str(target), "HEAD"]),
                shell="cmd",
                timeout_seconds=60,
                working_directory=str(self.settings.canonical_root),
                reason=f"selfdev isolated worktree for {issue_id}",
            )
        if not result.get("success"):
            raise RuntimeError(str(result.get("error_code") or "WORKTREE_CREATE_FAILED"))
        atomic_write_json(target / ".selfdev-candidate.json", {
            "issue_id": issue_id,
            "branch": branch,
            "canonical_root_name": self.settings.canonical_root.name,
        })
        return target

    async def diff(self, candidate: Path) -> str:
        candidate = contained_path(self.worktrees_root, candidate.relative_to(self.worktrees_root))
        result = await self.shell.execute(
            _cmd(["git", "diff", "--no-ext-diff", "--"]),
            shell="cmd",
            timeout_seconds=30,
            working_directory=str(candidate),
            reason="selfdev candidate diff read-only",
        )
        if not result.get("success"):
            raise RuntimeError("CANDIDATE_DIFF_FAILED")
        return str(result.get("stdout") or "")


class CodeWorker:
    """Applies schema-validated file content; LLM text is never a shell command."""

    ALLOWED_SUFFIXES = {
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".rs", ".json",
        ".yaml", ".yml", ".toml", ".md", ".css", ".html", ".ps1",
    }
    BLOCKED_PARTS = {
        ".git", ".env", ".venv", "venv", "data", "logs", "secrets",
        "credentials", "node_modules", "target", "dist", "build",
    }
    SECRET_PATTERNS = (
        re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
        re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_-]{16,}"),
        re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*[\"'][^<>\"']{8,}[\"']"),
    )

    def __init__(self, settings: SelfDevSettings) -> None:
        self.settings = settings

    def apply(self, candidate_root: Path, bundle: PatchBundle) -> list[str]:
        candidate_root = candidate_root.resolve()
        if self.settings.workspace.resolve() not in candidate_root.parents:
            raise ValueError("candidate must live inside SelfDev workspace")
        limit = self.settings.max_files_low_risk
        if len(bundle.changes) > limit:
            raise ValueError("LOW_RISK_FILE_LIMIT_EXCEEDED")
        changed_lines = sum(change.content.count("\n") + 1 for change in bundle.changes)
        if changed_lines > self.settings.max_diff_lines_low_risk:
            raise ValueError("LOW_RISK_DIFF_LIMIT_EXCEEDED")
        changed: list[str] = []
        for change in bundle.changes:
            target = contained_path(candidate_root, change.path)
            relative_parts = {part.casefold() for part in Path(change.path).parts}
            if relative_parts & self.BLOCKED_PARTS or target.suffix.casefold() not in self.ALLOWED_SUFFIXES:
                raise PermissionError(f"SELFDEV_PATH_BLOCKED:{change.path}")
            if any(pattern.search(change.content) for pattern in self.SECRET_PATTERNS):
                raise PermissionError(f"SELFDEV_SECRET_BLOCKED:{change.path}")
            if change.operation == "CREATE" and target.exists():
                raise FileExistsError(change.path)
            if change.operation == "UPDATE":
                if not target.is_file() or not change.expected_sha256:
                    raise ValueError(f"EXPECTED_HASH_REQUIRED:{change.path}")
                current = hashlib.sha256(target.read_bytes()).hexdigest()
                if current.casefold() != change.expected_sha256.casefold():
                    raise RuntimeError(f"SOURCE_CHANGED:{change.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            temporary.write_text(change.content, encoding="utf-8", newline="\n")
            os.replace(temporary, target)
            changed.append(change.path)
        return changed


class TestCommandRunner:
    """Allowlisted dev commands routed exclusively through system_shell."""

    ALLOWED = (
        re.compile(r"^(?:py|python)(?:\.exe)?\s+-[Bb]?m\s+(?:pytest|compileall)\b", re.I),
        re.compile(r"^npm(?:\.cmd)?\s+(?:test|run\s+(?:build|test(?::[\w-]+)?))\b", re.I),
        re.compile(r"^cargo\s+(?:check|test)\b", re.I),
        re.compile(r"^git\s+(?:diff|status|rev-parse)\b", re.I),
    )

    def __init__(self, shell: ShellService) -> None:
        self.shell = shell

    async def run(self, command: str, cwd: Path, timeout_seconds: int, reason: str) -> dict[str, Any]:
        if "\n" in command or "\r" in command or not any(pattern.search(command.strip()) for pattern in self.ALLOWED):
            raise PermissionError("SELFDEV_COMMAND_NOT_ALLOWLISTED")
        return await self.shell.execute(
            command,
            shell="cmd",
            timeout_seconds=timeout_seconds,
            working_directory=str(cwd),
            reason=reason[:500],
        )
