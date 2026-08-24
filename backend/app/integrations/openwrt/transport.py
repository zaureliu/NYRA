from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class OpenWrtTransport(ABC):
    """Transport boundary for future ubus/API/SSH read-only implementations."""

    @abstractmethod
    async def query(self, namespace: str, method: str) -> dict[str, Any]: ...


class OpenWrtReadOnlyAdapter:
    ALLOWED_QUERIES = {
        ("system", "info"),
        ("network.interface", "dump"),
        ("network.device", "status"),
        ("dhcp", "ipv4leases"),
        ("hostapd", "get_clients"),
    }

    def __init__(self, transport: OpenWrtTransport) -> None:
        self.transport = transport

    async def query(self, namespace: str, method: str) -> dict[str, Any]:
        if (namespace, method) not in self.ALLOWED_QUERIES:
            raise PermissionError("Consulta OpenWrt fora da allowlist read-only")
        return await self.transport.query(namespace, method)

