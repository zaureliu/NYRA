"""SSH-backed host adapters (OpenWrt/Linux/Windows) over the existing Trusted SSH layer.

These adapters never open their own SSH connection: every command goes through
``RemoteShellService.execute``, which enforces the Trusted Host Registry,
capability checks, risk classification, approvals, history, redaction and
auditing. Adapters only normalize outputs into structured data.
"""

from __future__ import annotations

import json
from typing import Any

from app.tools.remote_shell import RemoteShellService


class SshAdapterError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_ERROR_MAP = {
    "UNKNOWN_TRUSTED_HOST": "HOMELAB_HOST_UNKNOWN",
    "HOST_DISABLED": "HOMELAB_HOST_DISABLED",
    "SSH_AUTHENTICATION_FAILED": "REMOTE_AUTH_FAILED",
    "REMOTE_SHELL_DISABLED": "CAPABILITY_UNAVAILABLE",
    "CAPABILITY_DENIED": "CAPABILITY_UNAVAILABLE",
    "SSH_CREDENTIALS_MISSING": "REMOTE_AUTH_MISSING",
}


class SshHostAdapter:
    """Base plumbing shared by platform-specific adapters."""

    platform_label = "ssh"

    def __init__(self, remote_shell: RemoteShellService, host_id: str, timeout_seconds: int = 10) -> None:
        self.remote_shell = remote_shell
        self.host_id = host_id
        self.timeout_seconds = timeout_seconds

    async def run(self, command: str, *, timeout_seconds: int | None = None, reason: str = "") -> str:
        result = await self.remote_shell.execute(
            self.host_id,
            command,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            reason=reason or f"homelab:{self.platform_label}:read",
        )
        if not result.get("success"):
            code = str(result.get("error_code") or "REMOTE_EXECUTION_FAILED")
            message = str(result.get("message") or "O comando remoto falhou.")
            raise SshAdapterError(_ERROR_MAP.get(code, "REMOTE_EXECUTION_FAILED"), message)
        return str(result.get("stdout") or "")

    @staticmethod
    def parse_json_output(text: str) -> Any:
        try:
            return json.loads(text.strip())
        except ValueError:
            return None


def to_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
