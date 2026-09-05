"""Persistent Elevated Broker sessions (spec Parte E §100-§114).

One legitimate UAC consent opens a bounded admin session:
    §103   session_id/user/started_at/expires_at/capabilities;
    §102   no per-command UAC while an authorized session lives;
    §104   hard TTL, both client and server side;
    §105   never permanent by default;
    §106   UAC itself stays legitimate (no bypass);
    §107   IPC is a LOCAL-only named pipe;
    §108   the pipe DACL is restricted to the current user;
    §109   session token is ephemeral (in-memory only, random per session);
    §111   the risk classifier STILL applies to every command;
    §112   DESTRUCTIVE/CRITICAL still require their own approval.

The elevated host is a PowerShell NamedPipeServerStream started once via
Start-Process -Verb RunAs (same legitimate path as tools.elevated_broker).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.tools.shell_executor import decode_output
from app.tools.shell_risk import ShellRiskClassifier
from app.tools.shell_models import ShellRiskLevel
from app.tools.redaction import redact_secrets

_HOST_SCRIPT_TEMPLATE = r"""
param($PipeName, $Token, $TtlSeconds)
$ErrorActionPreference = 'Continue'
try {
  $userSid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User
  $pipeSecurity = New-Object System.IO.Pipes.PipeSecurity
  $allowRule = New-Object System.IO.Pipes.PipeAccessRule(
      $userSid,
      [System.Security.Principal.PipeAccessRights]'Read,Write',
      [System.Security.AccessControl.AccessControlType]::Allow)
  $pipeSecurity.AddAccessRule($allowRule)

  $deadline = (Get-Date).AddSeconds([int]$TtlSeconds)

  function Read-Exact([System.IO.Pipes.NamedPipeServerStream]$stream, [int]$count) {
    $buffer = New-Object byte[] $count
    $read = 0
    while ($read -lt $count) {
      $chunk = $stream.Read($buffer, $read, $count - $read)
      if ($chunk -le 0) { throw 'EOF' }
      $read += $chunk
    }
    return ,$buffer
  }

  function Write-Message([System.IO.Pipes.NamedPipeServerStream]$stream, [string]$text) {
    $payload = [Text.Encoding]::UTF8.GetBytes($text)
    $header = [BitConverter]::GetBytes([int]$payload.Length)
    $stream.Write($header, 0, 4)
    $stream.Write($payload, 0, $payload.Length)
    $stream.Flush()
  }

  while ((Get-Date) -lt $deadline) {
    $server = New-Object System.IO.Pipes.NamedPipeServerStream(
        $PipeName,
        [System.IO.Pipes.PipeDirection]::InOut,
        1,
        [System.IO.Pipes.PipeTransmissionMode]::Byte,
        [System.IO.Pipes.PipeOptions]::None,
        0, 0, $pipeSecurity)
    try {
      $remainingMs = [int][Math]::Max(1000, (($deadline - (Get-Date)).TotalSeconds * 1000))
      $connectTask = $server.WaitForConnectionAsync()
      if (-not $connectTask.Wait($remainingMs)) { break }
      $header = Read-Exact $server 4
      $length = [BitConverter]::ToInt32($header, 0)
      if ($length -le 0 -or $length -gt 2097152) { break }
      $body = Read-Exact $server $length
      $request = [Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json
      if ($request.token -ne $Token) {
        Write-Message $server '{"error":"TOKEN_MISMATCH"}'
        continue
      }
      if ($request.op -eq 'shutdown') {
        Write-Message $server '{"ok":true,"shutdown":true}'
        break
      }
      $command = [string]$request.command
      $shellKind = if ($request.shell) { [string]$request.shell } else { 'powershell' }
      $timeoutSec = if ($request.timeout_seconds) { [double]$request.timeout_seconds } else { 60 }
      $startedAt = [Diagnostics.Stopwatch]::StartNew()
      $exitCode = 0
      $stdoutText = ''
      $stderrText = ''
      $timedOut = $false
      try {
        if ($shellKind -eq 'cmd') {
          $psi = New-Object Diagnostics.ProcessStartInfo
          $psi.FileName = "$env:ComSpec"
          $psi.Arguments = "/d /s /c `"$command`""
          $psi.UseShellExecute = $false
          $psi.RedirectStandardOutput = $true
          $psi.RedirectStandardError = $true
          $proc = Diagnostics.Process::Start($psi)
          if (-not $proc.WaitForExit((New-TimeSpan -Seconds $timeoutSec).TotalMilliseconds)) {
            $timedOut = $true
            try { $proc.Kill($true) } catch { try { $proc.Kill() } catch {} }
          } else {
            $exitCode = $proc.ExitCode
          }
          if (-not $timedOut) {
            $stdoutText = $proc.StandardOutput.ReadToEnd()
            $stderrText = $proc.StandardError.ReadToEnd()
          }
        } else {
          $job = Start-Job -ScriptBlock ([scriptblock]::Create("Set-Location `$env:KAZUMI_SESSION_CWD; " + $command))
          if (-not (Wait-Job $job -Timeout $timeoutSec)) {
            $timedOut = $true
            Stop-Job $job
          }
          if (-not $timedOut) {
            $stdoutText = (Receive-Job $job | Out-String)
            $exitCode = 0
            $jobState = $job.State
            if ($jobState -eq 'Failed') { $exitCode = 1 }
          }
          Remove-Job $job -Force -ErrorAction SilentlyContinue
        }
      } catch {
        $stderrText = $_.Exception.Message
        $exitCode = 1
      }
      $startedAt.Stop()
      $response = @{
        ok = -not $timedOut
        exit_code = $exitCode
        stdout = $stdoutText.Substring(0, [Math]::Min(60000, $stdoutText.Length))
        stderr = $stderrText.Substring(0, [Math]::Min(20000, $stderrText.Length))
        timed_out = $timedOut
        duration_ms = [int]$startedAt.ElapsedMilliseconds
      } | ConvertTo-Json -Compress -Depth 3
      Write-Message $server $response
    } catch {
      # connection-level failure: keep serving until TTL
    } finally {
      try { if ($server.IsConnected) { $server.Disconnect() } } catch {}
      $server.Dispose()
    }
  }
  Write-Output ('SESSION_ENDED')
} catch {
  Write-Output ('HOST_ERROR: ' + $_.Exception.Message)
}
"""


@dataclass
class ElevatedSession:
    session_id: str
    user: str
    started_at: float
    expires_at: float
    capabilities: list[str]
    pipe_name: str
    token: str = field(repr=False)  # ephemeral, never logged/persisted (§109)
    host_pid: int | None = None

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def public_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user": self.user,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "capabilities": self.capabilities,
            "expired": self.expired(),
            "host_alive_hint": self.host_pid,
        }


class ElevatedSessionManager:
    def __init__(self, approvals=None, *, default_ttl_seconds: int = 300,
                 max_ttl_seconds: int = 900) -> None:
        self.approvals = approvals
        self.default_ttl_seconds = max(60, min(default_ttl_seconds, max_ttl_seconds))
        self.max_ttl_seconds = max_ttl_seconds
        self.classifier = ShellRiskClassifier()
        self._sessions: dict[str, ElevatedSession] = {}

    # ------------------------------------------------------------------ open/close
    async def open(self, *, reason: str, ttl_seconds: int | None = None,
                   approval_id: str | None = None) -> dict:
        ttl = max(60, min(int(ttl_seconds or self.default_ttl_seconds), self.max_ttl_seconds))
        decision = self._require_approval(
            description=f"Abrir sessão administrativa (UAC legítimo) por {ttl}s — {reason[:120]}",
            risk="ELEVATED", approval_id=approval_id,
        )
        if decision is not None:
            return decision
        self._sweep()
        user = _current_user()
        pipe_name = f"kazumi-elevated-{secrets.token_hex(8)}"
        token = secrets.token_urlsafe(32)
        script_path = Path(tempfile.gettempdir()) / f"kazumi-elevated-host-{pipe_name[-8:]}.ps1"
        script_path.write_text(_HOST_SCRIPT_TEMPLATE, encoding="utf-8-sig")
        launch_ps = (
            "$p = Start-Process -FilePath powershell.exe "
            f"-ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','{script_path}','{pipe_name}','{token}',{ttl}) "
            "-Verb RunAs -PassThru; if ($p) { $p.Id } else { 'CANCELLED' }"
        )
        try:
            completed = await asyncio.to_thread(
                lambda: subprocess.run(  # noqa: S603
                    ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", launch_ps],
                    capture_output=True, timeout=180,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )
            stdout = decode_output(completed.stdout).strip()
            stderr = decode_output(completed.stderr).strip().casefold()
            if "CANCELLED" in stdout or "canceled by the user" in stderr:
                return {"success": False, "error_code": "UAC_CANCELLED",
                        "message": "Operador cancelou o UAC; nenhuma sessão aberta."}
            host_pid = None
            for line in stdout.splitlines():
                cleaned = line.strip()
                if cleaned.isdigit():
                    host_pid = int(cleaned)
                    break
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"success": False, "error_code": "ELEVATION_FAILED",
                    "message": f"Falha ao elevar host da sessão: {type(exc).__name__}"}
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass
        session = ElevatedSession(
            session_id=f"esess_{secrets.token_hex(8)}",
            user=user,
            started_at=time.time(),
            expires_at=time.time() + ttl,
            capabilities=["shell:powershell", "shell:cmd"],
            pipe_name=pipe_name,
            token=token,
            host_pid=host_pid,
        )
        self._sessions[session.session_id] = session
        ready = await asyncio.to_thread(self._wait_pipe_ready, session, 25.0)
        if not ready:
            self._sessions.pop(session.session_id, None)
            return {"success": False, "error_code": "PIPE_NOT_READY",
                    "message": "Host elevado não abriu o pipe no tempo esperado."}
        return {"success": True, **session.public_dict(), "ttl_seconds": ttl}

    async def close(self, session_id: str) -> dict:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return {"success": False, "error_code": "SESSION_NOT_FOUND"}
        await asyncio.to_thread(self._send_request, session, {"op": "shutdown"}, 10.0)
        return {"success": True, "closed": session_id}

    # ------------------------------------------------------------------ execute
    async def execute(self, session_id: str, command: str, *, shell: str = "powershell",
                      timeout_seconds: int = 60, approval_id: str | None = None) -> dict:
        session = self._get_live_session(session_id)
        if isinstance(session, dict):
            return session
        assessment = self.classifier.classify(command, shell)
        level = ShellRiskLevel(assessment.level.value)
        effective_timeout = max(1, min(int(timeout_seconds), 600))
        if level in {ShellRiskLevel.DESTRUCTIVE, ShellRiskLevel.CRITICAL}:
            # §112: broker elevado NÃO remove policies.
            decision = self._require_approval(
                description=(
                    f"[sessão {session_id}] comando destrutivo: "
                    f"{redact_secrets(command[:160])}"
                ),
                risk=level.value, approval_id=approval_id,
                binding_digest=self._binding_digest(
                    session_id, command, shell, effective_timeout,
                ),
            )
            if decision is not None:
                return decision
        response = await asyncio.to_thread(
            self._send_request, session,
            {"command": command, "shell": shell, "timeout_seconds": effective_timeout},
            timeout=min(float(effective_timeout) + 20.0, 640.0),
        )
        if not response.get("_transport_ok"):
            return {"success": False, "error_code": response.get("error", "PIPE_UNAVAILABLE"),
                    "message": "Falha de IPC com o host elevado.", "detail": response}
        result = {
            "success": bool(response.get("ok")),
            "risk_level": level.value,
            "reasons": assessment.reasons[:6],
            "exit_code": response.get("exit_code"),
            "stdout": response.get("stdout", ""),
            "stderr": response.get("stderr", ""),
            "timed_out": bool(response.get("timed_out")),
            "duration_ms": response.get("duration_ms"),
            "effect_verified": not response.get("timed_out", True),
            "verification_status": "EXECUTED",
        }
        return result

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        self._sweep()
        return {
            "success": True,
            "active_sessions": [item.public_dict() for item in self._sessions.values()],
            "default_ttl_seconds": self.default_ttl_seconds,
            "max_ttl_seconds": self.max_ttl_seconds,
        }

    # ---------------------------------------------------------------- internals
    def _get_live_session(self, session_id: str) -> ElevatedSession | dict:
        session = self._sessions.get(session_id)
        if session is None:
            return {"success": False, "error_code": "SESSION_NOT_FOUND"}
        if session.expired():
            self._sessions.pop(session_id, None)
            return {"success": False, "error_code": "SESSION_EXPIRED"}
        return session

    @staticmethod
    def _wait_pipe_ready(session: ElevatedSession, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                handle = open(rf"\\.\pipe\{session.pipe_name}", "r+b", buffering=0)  # noqa: SIM115
                handle.close()
                return True
            except FileNotFoundError:
                time.sleep(0.5)
            except OSError:
                time.sleep(0.5)
        return False

    @staticmethod
    def _send_request(session: ElevatedSession, payload: dict, timeout: float) -> dict:
        """Length-prefixed JSON over the local pipe; threaded read with deadline."""
        result: dict = {"_transport_ok": False, "error": "TIMEOUT"}

        def worker() -> None:
            try:
                with open(rf"\\.\pipe\{session.pipe_name}", "r+b", buffering=0) as pipe:
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    pipe.write(len(body).to_bytes(4, "little") + body)
                    header = b""
                    while len(header) < 4:
                        chunk = pipe.read(4 - len(header))
                        if not chunk:
                            raise OSError("eof")
                        header += chunk
                    length = int.from_bytes(header, "little")
                    if length <= 0 or length > 262144:
                        raise ValueError("bad_length")
                    chunks = []
                    remaining = length
                    while remaining > 0:
                        chunk = pipe.read(min(remaining, 65536))
                        if not chunk:
                            raise OSError("eof")
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    document = json.loads(b"".join(chunks).decode("utf-8"))
                    result.clear()
                    result.update(document)
                    result["_transport_ok"] = True
            except Exception as exc:  # noqa: BLE001 - transport errors become payloads
                result.clear()
                result.update({"_transport_ok": False, "error": type(exc).__name__})

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=max(5.0, min(timeout, 700.0)))
        return result

    @staticmethod
    def _binding_digest(*values) -> str:
        material = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _require_approval(self, *, description: str, risk: str,
                          approval_id: str | None, binding_digest: str = "") -> dict | None:
        from app.tools.shell_models import ShellRiskLevel as _SRL

        if self.approvals is None:
            return {"success": False, "error_code": "APPROVAL_REQUIRED"}
        approval_command = f"{description} params_sha256={binding_digest or 'none'}"
        fingerprint = self.approvals.fingerprint(
            approval_command, "elevated_broker", "", 300, target="local",
        )
        if not approval_id:
            record = self.approvals.request(
                command=approval_command, shell="elevated_broker", working_directory="",
                timeout_seconds=300, risk_level=_SRL(risk), target="local",
                fingerprint=fingerprint,
            )
            return {"success": False, "error_code": "APPROVAL_REQUIRED",
                    "approval_required": True, "approval_id": record.approval_id}
        granted, reason = self.approvals.consume(approval_id, fingerprint)
        if not granted:
            return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
        return None

    def _sweep(self) -> None:
        expired = [sid for sid, item in self._sessions.items() if item.expired()]
        for sid in expired:
            self._sessions.pop(sid, None)


def _current_user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"
