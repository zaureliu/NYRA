"""Shared contracts for homelab integrations."""

from __future__ import annotations

from typing import Any


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
