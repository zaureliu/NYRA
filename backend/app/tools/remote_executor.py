from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import time

from app.network_aliases import NetworkHostAlias


@dataclass
class RawRemoteResult:
    executable: str | None
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: float
    timed_out: bool = False
    launch_error: str | None = None


class OpenSSHExecutor:
    """Finite OpenSSH command execution with strict host-key verification."""

    def resolve_executable(self) -> str | None:
        candidates = ("ssh.exe", "ssh") if os.name == "nt" else ("ssh",)
        return next((path for item in candidates if (path := shutil.which(item))), None)

    async def execute(
        self,
        host: NetworkHostAlias,
        command: str,
        *,
        connect_timeout_seconds: int,
        command_timeout_seconds: int,
        known_hosts_path: Path,
        private_key_path: Path | None,
    ) -> RawRemoteResult:
        executable = self.resolve_executable()
        if not executable:
            return RawRemoteResult(None, None, b"", b"", 0, launch_error="OpenSSH client is unavailable")
        remote = host.remote_shell
        args = [
            executable,
            "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts_path}",
            "-o", f"ConnectTimeout={connect_timeout_seconds}",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=1",
            "-p", str(remote.port),
        ]
        if private_key_path is not None:
            args.extend(["-o", "IdentitiesOnly=yes", "-i", str(private_key_path)])
        args.extend([f"{remote.username}@{host.address}", command])
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=flags,
            )
        except (OSError, ValueError) as exc:
            return RawRemoteResult(
                executable, None, b"", b"", round((time.perf_counter() - started) * 1000, 2),
                launch_error=f"{type(exc).__name__}: {exc}",
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), command_timeout_seconds)
            return RawRemoteResult(
                executable, process.returncode, stdout, stderr,
                round((time.perf_counter() - started) * 1000, 2),
            )
        except TimeoutError:
            await self._terminate(process)
            return RawRemoteResult(
                executable, process.returncode, b"", b"",
                round((time.perf_counter() - started) * 1000, 2), timed_out=True,
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            raise

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt" and process.pid:
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await asyncio.wait_for(killer.wait(), 5)
            except (OSError, TimeoutError):
                process.kill()
        else:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            pass
