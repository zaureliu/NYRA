"""Process lifecycle for OWNED PROCESS services (Windows-first, psutil identity)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess

import psutil

from app.runtime.models import IdentityRecord


class ManagedProcess:
    def __init__(self, service_id: str, pid: int, create_time: float, exe_path: str, cmdline_fingerprint: str) -> None:
        self.service_id = service_id
        self.identity = IdentityRecord(
            pid=pid, create_time=round(create_time, 3), exe_path=exe_path,
            cmdline_fingerprint=cmdline_fingerprint,
        )
        self.started_at = datetime.now(timezone.utc)

    def alive(self) -> bool:
        try:
            process = psutil.Process(self.identity.pid)
            return process.is_running() and abs(process.create_time() - self.identity.create_time) < 0.5
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def uptime_seconds(self) -> float | None:
        if not self.alive():
            return None
        try:
            return max(0.0, datetime.now(timezone.utc).timestamp() - self.identity.create_time)
        except (OSError, ValueError, OverflowError):
            return None


def cmdline_fingerprint(argv: list[str]) -> str:
    import hashlib
    import json

    normalized = [str(part).casefold().replace("/", "\\")[-120:] for part in argv]
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


class ProcessManager:
    """Spawn detached long-running processes; stdout/stderr go to a rotating log file."""

    def __init__(self) -> None:
        self.tracked: dict[str, ManagedProcess] = {}

    async def spawn(self, service_id: str, argv: list[str], working_directory: Path, log_path: Path) -> ManagedProcess:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        with open(log_path, "ab", buffering=0) as handle:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(working_directory),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                creationflags=creationflags,
                close_fds=True,
            )
        fingerprint = cmdline_fingerprint(argv)
        exe_path = ""
        create_time = float(process.pid)
        try:
            probe = psutil.Process(process.pid)
            create_time = probe.create_time()
            exe_path = probe.exe() or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
        managed = ManagedProcess(service_id, process.pid, create_time, exe_path, fingerprint)
        self.tracked[service_id] = managed
        return managed

    def get(self, service_id: str) -> ManagedProcess | None:
        managed = self.tracked.get(service_id)
        if managed and not managed.alive():
            self.tracked.pop(service_id, None)
            return None
        return managed

    def find_by_identity(self, expected: IdentityRecord) -> ManagedProcess | None:
        for managed in list(self.tracked.values()):
            if managed.alive() and managed.identity.matches(expected):
                return managed
        return None

    async def graceful_stop(self, managed: ManagedProcess, grace_seconds: float = 5.0) -> bool:
        pid = managed.identity.pid
        if not managed.alive():
            return True
        forced = await self._taskkill(pid, force=False)
        deadline = asyncio.get_event_loop().time() + grace_seconds
        while asyncio.get_event_loop().time() < deadline:
            if not managed.alive():
                return True
            await asyncio.sleep(0.25)
        if not managed.alive():
            return True
        if not forced:
            await self._taskkill(pid, force=False)
            await asyncio.sleep(0.5)
        if managed.alive():
            await self._taskkill(pid, force=True, tree=True)
            await asyncio.sleep(0.5)
        return not managed.alive()

    @staticmethod
    async def _taskkill(pid: int, force: bool, tree: bool = False) -> bool:
        args = ["taskkill.exe", "/PID", str(pid)]
        if tree:
            args.append("/T")
        if force:
            args.append("/F")
        try:
            killer = await asyncio.create_subprocess_exec(
                *args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            await asyncio.wait_for(killer.wait(), 10)
            return killer.returncode == 0
        except (OSError, TimeoutError):
            return False


def rotate_log_file(path: Path, max_bytes: int, backup_count: int) -> None:
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for index in range(backup_count - 1, 0, -1):
            source = path.with_suffix(path.suffix + f".{index}")
            target = path.with_suffix(path.suffix + f".{index + 1}")
            if source.exists():
                target.unlink(missing_ok=True)
                source.rename(target)
        path.rename(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


__all__ = ["ManagedProcess", "ProcessManager", "cmdline_fingerprint", "rotate_log_file"]
