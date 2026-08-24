"""Elevated Operations Broker: legitimate UAC consent flow for admin commands.

No UAC bypass, no credential handling, no network exposure. When a command
requires elevation and the operator has granted the linked approval, the broker
re-invokes it via Start-Process -Verb RunAs. Windows itself shows the consent
prompt on the interactive session; if the operator declines or cancels, the
command simply does not run (ELEVATION_CANCELLED). Output is written to temp
files by the elevated child process itself because Start-Process forbids
combining -Verb RunAs with stream redirection parameters.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from app.tools.shell_executor import decode_output


_ACCESS_DENIED = (
    "acesso negado", "access denied", "permission denied",
    "requires elevation", "elevation is required",
    "requested operation requires elevation",
    "a operação solicitada requer elevação",
    "deve ser executado com elevação",
)


def is_access_denied_output(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".casefold()
    return any(token in combined for token in _ACCESS_DENIED)


def process_is_elevated() -> bool:
    """True when the backend process runs under an administrator token."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def build_elevated_script(command: str, shell: str, out_path: Path, err_path: Path) -> str:
    """PowerShell bootstrap executed AS ADMIN: runs the command, captures outputs."""
    out = json.dumps(str(out_path))
    err = json.dumps(str(err_path))
    if shell == "cmd":
        inner_cmd = command.replace('"', '\\"')
        return (
            "$ErrorActionPreference='Continue';"
            f"cmd.exe /d /s /c \"\"{inner_cmd}\"\" >{out} 2>{err};"
            "exit $LASTEXITCODE"
        )
    return (
        "$ErrorActionPreference='Continue';"
        f"$__nyra_oem=[Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage);"
        "[Console]::OutputEncoding=$__nyra_oem;$OutputEncoding=$__nyra_oem;"
        f"$__o={out};$__e={err};"
        "$__r=& {" + command + "} 2>&1 | Out-String -Stream;"
        "$__ok=$?;$__native=$LASTEXITCODE;"
        "[IO.File]::WriteAllText($__o,$__r,[Text.UTF8Encoding]::new($false));"
        "$__err=[Console]::Error.ToString();"
        "[IO.File]::WriteAllText($__e,$__err,[Text.UTF8Encoding]::new($false));"
        "$__code=if($null -ne $__native){[int]$__native}elseif($__ok){0}else{1};"
        "exit $__code"
    )


def run_elevated(
    command: str,
    shell: str,
    timeout_seconds: int,
    working_directory: Path,
) -> dict:
    """Execute one approved command through UAC consent; returns raw-shaped data."""
    started = time.perf_counter()
    run_id = os.urandom(6).hex()
    out_path = Path(tempfile.gettempdir()) / f"nyra-elevated-{run_id}-out.txt"
    err_path = Path(tempfile.gettempdir()) / f"nyra-elevated-{run_id}-err.txt"
    try:
        script = build_elevated_script(command, shell, out_path, err_path)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command",
                (
                    "$p = Start-Process -FilePath powershell.exe "
                    "-ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-Command',"
                    + json.dumps(script)
                    + ") -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
                ),
            ],
            cwd=str(working_directory),
            capture_output=True,
            timeout=timeout_seconds,
            creationflags=creationflags,
        )
        exit_code = completed.returncode
        stderr_text = decode_output(completed.stderr).casefold() if completed.stderr else ""
        # 1223 = ERROR_CANCELLED: operator dismissed the UAC consent dialog.
        cancelled = exit_code == 1223 or "canceled by the user" in stderr_text or "cancelada pelo usuário".casefold() in stderr_text
        stdout = out_path.read_bytes() if out_path.exists() else b""
        stderr = err_path.read_bytes() if err_path.exists() else b""
        return {
            "executable": "powershell.exe",
            "exit_code": None if cancelled else exit_code,
            "stdout": decode_output(stdout),
            "stderr": decode_output(stderr),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "timed_out": False,
            "launch_error": None if not cancelled else "UAC_CANCELLED",
        }
    except TimeoutError:
        return {
            "executable": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "timed_out": True,
            "launch_error": None,
        }
    except OSError as exc:
        return {
            "executable": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "timed_out": False,
            "launch_error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)
