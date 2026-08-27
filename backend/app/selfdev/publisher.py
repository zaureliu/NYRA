from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable

from app.selfdev.models import SelfDevRisk, SelfDevSettings
from app.selfdev.storage import ResourceLock, contained_path
from app.selfdev.validation import SecurityScanner
from app.selfdev.workspace import ShellService


def _cmd(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments)


class GitHubPublisher:
    PUBLIC_ROOTS = {"backend", "config", "desktop", "docs", "frontend", "identity", "live2d", "scripts", "watchdog"}
    ROOT_FILES = {
        ".env.example", ".gitignore", "AGENTS.md", "CHANGELOG.md", "LICENSE",
        "README.md", "build-nyra.ps1", "package.json", "start-nyra.ps1",
    }
    BLOCKED_PARTS = {".git", "data", "logs", "cache", "downloads", "models", "node_modules", "target", "dist", "build", ".venv", "venv", ".tmp", ".test-temp"}
    BLOCKED_SUFFIXES = {".exe", ".msi", ".pdb", ".dmp", ".db", ".sqlite", ".log"}

    def __init__(self, settings: SelfDevSettings, shell: ShellService, scanner: SecurityScanner) -> None:
        self.settings = settings
        self.shell = shell
        self.scanner = scanner
        self.lock = ResourceLock(settings.workspace / "locks" / "github_publish.lock")

    def sync_public_snapshot(self) -> list[str]:
        source = self.settings.canonical_root.resolve()
        destination = self.settings.public_snapshot.resolve()
        if not (destination / ".git").is_dir():
            raise RuntimeError("PUBLIC_SNAPSHOT_GIT_MISSING")
        copied: list[str] = []
        for path in self._public_files(source):
            relative = path.relative_to(source)
            target = contained_path(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.selfdev.tmp")
            shutil.copy2(path, temporary)
            os.replace(temporary, target)
            copied.append(relative.as_posix())
        return copied

    def scan_public(self) -> list[str]:
        return self.scanner.scan(self.settings.public_snapshot, public=True)

    async def publish(self, issue_id: str, title: str, risk: SelfDevRisk, *, approval_id: str | None = None) -> dict[str, Any]:
        if not self.settings.auto_publish_github:
            return {"status": "OFF", "error_code": "AUTO_PUBLISH_DISABLED"}
        if risk != SelfDevRisk.LOW:
            return {"status": "BLOCKED", "error_code": "PUBLISH_RISK_BLOCKED"}
        status = await self.shell.execute(
            _cmd(["git", "status", "--porcelain"]), shell="cmd", timeout_seconds=20,
            working_directory=str(self.settings.public_snapshot), reason="SelfDev public snapshot status",
        )
        if not status.get("success"):
            return {"status": "FAILED", "error_code": "PUBLIC_STATUS_FAILED"}
        if str(status.get("stdout") or "").strip():
            return {"status": "BLOCKED", "error_code": "PUBLIC_SNAPSHOT_DIRTY"}
        findings = self.scan_public()
        if findings:
            return {"status": "BLOCKED", "error_code": "PUBLICATION_BLOCKED", "findings": findings[:100]}
        source_files = [path.relative_to(self.settings.canonical_root).as_posix() for path in self._public_files(self.settings.canonical_root)]
        source_findings = self.scanner.scan(self.settings.canonical_root, source_files, public=True)
        if source_findings:
            return {"status": "BLOCKED", "error_code": "SOURCE_PUBLICATION_BLOCKED", "findings": source_findings[:100]}
        self.sync_public_snapshot()
        findings = self.scan_public()
        if findings:
            return {"status": "BLOCKED", "error_code": "PUBLICATION_BLOCKED", "findings": findings[:100]}
        message = f"selfdev({issue_id}): {self._safe_title(title)}"
        with self.lock.held():
            add = await self.shell.execute(
                _cmd(["git", "add", "-A"]), shell="cmd", timeout_seconds=60,
                working_directory=str(self.settings.public_snapshot), approval_id=approval_id,
                reason=f"SelfDev stage sanitized public snapshot {issue_id}",
            )
            if not add.get("success"):
                return self._pending_or_failed(add)
            commit = await self.shell.execute(
                _cmd(["git", "commit", "-m", message]), shell="cmd", timeout_seconds=60,
                working_directory=str(self.settings.public_snapshot), approval_id=approval_id,
                reason=f"SelfDev commit validated public snapshot {issue_id}",
            )
            if not commit.get("success"):
                return self._pending_or_failed(commit)
            push = await self.shell.execute(
                _cmd(["git", "push", "origin", "main"]), shell="cmd", timeout_seconds=180,
                working_directory=str(self.settings.public_snapshot), approval_id=approval_id,
                reason=f"SelfDev publish validated LOW_RISK improvement {issue_id}",
            )
        if not push.get("success"):
            return self._pending_or_failed(push)
        return {"status": "PUBLISHED", "commit_output": str(commit.get("stdout") or "")[-500:]}

    def _public_files(self, root: Path) -> Iterable[Path]:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if relative.parts[0] not in self.PUBLIC_ROOTS and relative.as_posix() not in self.ROOT_FILES:
                continue
            parts = {part.casefold() for part in relative.parts}
            if parts & self.BLOCKED_PARTS or path.suffix.casefold() in self.BLOCKED_SUFFIXES:
                continue
            if path.name.casefold() == ".env" or path.name.casefold().startswith(".env.") and path.name != ".env.example":
                continue
            yield path

    @staticmethod
    def _safe_title(value: str) -> str:
        return re.sub(r"[^\w ._-]+", "", value, flags=re.UNICODE).strip()[:72] or "melhoria validada"

    @staticmethod
    def _pending_or_failed(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("approval_required"):
            return {"status": "PENDING", "error_code": "PUBLISH_APPROVAL_REQUIRED", "approval_id": result.get("approval_id")}
        return {"status": "FAILED", "error_code": result.get("error_code") or "PUBLISH_FAILED"}
