"""Homelab Control Plane data models.

Structured view of the operator's homelab: hosts with capabilities and
policies, normalized health states and probe results. Credentials never live
here; hosts reference a credentials_profile resolved elsewhere.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class HostType(StrEnum):
    OPENWRT = "openwrt"
    PROXMOX = "proxmox"
    HOME_ASSISTANT = "home_assistant"
    LINUX = "linux"
    WINDOWS = "windows"


class IntegrationKind(StrEnum):
    NONE = "none"
    PROXMOX_API = "proxmox_api"
    HOME_ASSISTANT_API = "home_assistant_api"
    TRUSTED_SSH = "trusted_ssh"
    WINDOWS_REMOTE = "windows_remote"


class HealthState(StrEnum):
    UNKNOWN = "UNKNOWN"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    UNREACHABLE = "UNREACHABLE"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    INTEGRATION_UNAVAILABLE = "INTEGRATION_UNAVAILABLE"
    DISABLED = "DISABLED"


class HostCapabilities(BaseModel):
    network: bool = True
    icmp: bool = True
    tcp_probes: list[int] = Field(default_factory=list)
    http_path: str = ""
    ssh: bool = False
    api: bool = False
    logs: bool = False
    services: bool = False
    virtual_machines: bool = False
    storage: bool = False
    wifi: bool = False


class HostDefinition(BaseModel):
    """One homelab host as registered in the unified registry."""

    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    display_name: str = Field(min_length=1, max_length=120)
    type: HostType
    address: str = Field(min_length=3, max_length=253)
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True
    capabilities: HostCapabilities = Field(default_factory=HostCapabilities)
    integration: IntegrationKind = IntegrationKind.NONE
    credentials_profile: str = ""
    health_policy: dict[str, Any] = Field(default_factory=dict)
    risk_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        import ipaddress
        import re

        candidate = value.strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
        if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", candidate):
            return candidate
        raise ValueError(f"Endereço de host inválido: {value!r}")

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for alias in value:
            text = alias.strip().casefold()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned


class HomelabRegistryFile(BaseModel):
    version: int = 1
    hosts: list[HostDefinition] = Field(default_factory=list)


class ProbeResult(BaseModel):
    kind: Literal["icmp", "tcp", "http", "dns"]
    success: bool
    latency_ms: float | None = None
    detail: str = ""


class HostHealth(BaseModel):
    """Normalized observation of one host at one point in time."""

    host_id: str
    address: str
    reachable: bool = False
    overall_state: HealthState = HealthState.UNKNOWN
    probes: list[ProbeResult] = Field(default_factory=list)
    integration_state: HealthState = HealthState.UNKNOWN
    integration_error_code: str | None = None
    integration_detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: float = 0.0
    cached: bool = False


class HomelabOverview(BaseModel):
    generated_at: float = 0.0
    cached: bool = False
    enabled: bool = True
    hosts: list[HostHealth] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class ActionDecision(BaseModel):
    action: str
    risk_level: Literal["READ_ONLY", "LOW_RISK", "ELEVATED", "DESTRUCTIVE", "CRITICAL"]
    requires_approval: bool
    reason: str = ""


def safe_url_host(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.hostname or ""
