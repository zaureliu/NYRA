"""Shared contracts for homelab integrations."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit


class IntegrationError(Exception):
    """Normalized integration failure carrying a stable machine error code.

    Codes follow spec §185 (PROXMOX_AUTH_MISSING, HA_AUTH_FAILED,
    CAPABILITY_UNAVAILABLE...). The LLM receives only ``message``; tracebacks
    stay in technical logs with redaction applied upstream.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.message}


def require_secure_credential_transport(base_url: str) -> str:
    """Allow plaintext credentials only to a literal loopback IP.

    Hostnames are rejected for HTTP so validation and the network client cannot
    resolve the same name to different addresses (DNS rebinding/TOCTOU).
    """
    value = (base_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise IntegrationError("INSECURE_CREDENTIAL_TRANSPORT", "URL de integração inválida.")
    if parsed.scheme == "https":
        return value
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise IntegrationError(
            "INSECURE_CREDENTIAL_TRANSPORT",
            "Credenciais em HTTP exigem IP loopback literal; use HTTPS para nomes de host.",
        ) from exc
    if not address.is_loopback:
        raise IntegrationError(
            "INSECURE_CREDENTIAL_TRANSPORT",
            "Credenciais em HTTP só podem ser enviadas para loopback; configure HTTPS.",
        )
    return value


class RemoteHostAdapter:
    """Abstract remote-management surface for future NYRA Remote Node backends.

    V1 ships SSH-based adapters only; WinRM and a dedicated NYRA Remote Node
    remain explicit capability gaps that must fail honestly instead of being
    simulated.
    """

    async def status(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def metrics(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def services(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError
