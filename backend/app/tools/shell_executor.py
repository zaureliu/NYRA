from __future__ import annotations

import asyncio
from dataclasses import dataclass
import locale
import os
from pathlib import Path
import shutil
import subprocess
import time


@dataclass
class RawShellResult:
    executable: str | None
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: float
    timed_out: bool = False
    launch_error: str | None = None


class ShellExecutor:
    def resolve_executable(self, shell: str) -> str | None:
        if shell == "powershell":
            candidates = ("pwsh.exe", "powershell.exe") if os.name == "nt" else ("pwsh", "powershell")
        elif shell == "cmd":
            candidates = ("cmd.exe",) if os.name == "nt" else ()
        else:
            return None
        return next((path for candidate in candidates if (path := shutil.which(candidate))), None)

    async def execute(
        self,
        command: str,
        shell: str,
        timeout_seconds: int,
        working_directory: Path,
    ) -> RawShellResult:
        executable = self.resolve_executable(shell)
        if not executable:
            return RawShellResult(
                executable=None,
                exit_code=None,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                launch_error=f"Shell {shell} não está disponível neste host.",
            )
        args = self._arguments(executable, shell, command)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(working_directory),
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except (OSError, ValueError) as exc:
            return RawShellResult(
                executable=executable,
                exit_code=None,
                stdout=b"",
                stderr=b"",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                launch_error=f"{type(exc).__name__}: {exc}",
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
            return RawShellResult(
                executable=executable,
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except TimeoutError:
            await self._terminate_tree(process)
            return RawShellResult(
                executable=executable,
                exit_code=process.returncode,
                stdout=b"",
                stderr=b"",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                timed_out=True,
            )
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            raise

    @staticmethod
    def _arguments(executable: str, shell: str, command: str) -> list[str]:
        if shell == "cmd":
            return [executable, "/D", "/S", "/C", command]
        wrapper = (
            "$__kazumi_oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage); "
            "[Console]::OutputEncoding = $__kazumi_oem; $OutputEncoding = $__kazumi_oem; "
            f"$__kazumi_output = & {{ {command} }}; "
            "$__kazumi_ok = $?; $__kazumi_native_exit = $LASTEXITCODE; "
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            "$__kazumi_output | Out-String -Stream; "
            "$__kazumi_exit = if ($null -ne $__kazumi_native_exit) { $__kazumi_native_exit } elseif ($__kazumi_ok) { 0 } else { 1 }; "
            "$host.SetShouldExit([int]$__kazumi_exit)"
        )
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", wrapper]

    async def _terminate_tree(self, process: asyncio.subprocess.Process) -> None:
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


def decode_output(value: bytes) -> str:
    if not value:
        return ""
    encodings = ["utf-8"]
    if os.name == "nt":
        try:
            import ctypes
            encodings.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
        except (AttributeError, OSError):
            pass
        encodings.extend(("mbcs", "cp1252", "cp850"))
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")
