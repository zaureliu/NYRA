from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address, ip_network
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


BRIDGE_VERSION = 1
NAMESPACE = "/integrations/kazumi"
MAX_EVENT_BYTES = 32 * 1024


class SentinelState(StrEnum):
    DISABLED = "DISABLED"
    DISCOVERING = "DISCOVERING"
    FOUND = "FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    OFFLINE = "OFFLINE"
    AUTH_FAILED = "AUTH_FAILED"
    INCOMPATIBLE = "INCOMPATIBLE"
    ERROR = "ERROR"


class SentinelSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    RECOVERY = "recovery"


class SentinelEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str = Field(default="resource", max_length=60)
    name: str = Field(default="", max_length=180)


class SentinelEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: int = Field(ge=1, le=1)
    event_id: str = Field(min_length=8, max_length=100)
    source: str = Field(pattern=r"^utamo-sentinel$")
    instance_id: str = Field(min_length=8, max_length=100)
    timestamp: datetime
    category: str = Field(max_length=60)
    type: str = Field(max_length=100)
    severity: SentinelSeverity
    title: str = Field(max_length=160)
    summary: str = Field(max_length=800)
    entity: SentinelEntity = Field(default_factory=SentinelEntity)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SentinelFingerprint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service: str = Field(pattern=r"^utamo-sentinel$")
    integration: str = Field(pattern=r"^kazumi$")
    status: str
    api_version: str
    sentinel_version: str
    instance_id: str
    capabilities: list[str]
    authentication_required: bool = True


class SentinelSettingsUpdate(BaseModel):
    enabled: bool = False
    auto_discovery: bool = True
    discovery_interval: int = Field(60, ge=15, le=3600)
    host: str = Field(default="", max_length=253)
    port: int = Field(5000, ge=1, le=65535)
    prefer_manual_host: bool = True
    voice_alerts: bool = True
    desktop_alerts: bool = True
    critical_only: bool = False
    store_event_history: bool = True
    create_episodic_memory: bool = False
    auto_reconnect: bool = True
    reconnect_backoff: list[int] = Field(default_factory=lambda: [1, 2, 5, 10, 30, 60], min_length=1, max_length=8)
    debug_mode: bool = False
    discovery_allowlist: list[str] = Field(default_factory=list, max_length=8)
    event_retention_days: int = Field(30, ge=1, le=365)
    alert_cooldown_seconds: int = Field(300, ge=10, le=86400)
    disconnect_grace_seconds: int = Field(7, ge=1, le=60)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "://" in value:
            parsed = urlsplit(value)
            try:
                parsed_port = parsed.port
                address = ip_address(parsed.hostname or "")
            except ValueError as error:
                raise ValueError("Host Sentinel exige IP local literal") from error
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or parsed.path not in {"", "/"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not (address.is_private or address.is_loopback or address.is_link_local)
                or (parsed.scheme.lower() == "http" and not address.is_loopback)
            ):
                raise ValueError("Host Sentinel inválido")
            _ = parsed_port
            return value.rstrip("/")
        try:
            address = ip_address(value)
        except ValueError as error:
            raise ValueError("Host Sentinel exige IP local literal") from error
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("Host Sentinel deve pertencer à rede local")
        return value

    @field_validator("reconnect_backoff")
    @classmethod
    def validate_backoff(cls, values: list[int]) -> list[int]:
        if any(value < 1 or value > 300 for value in values):
            raise ValueError("Backoff deve ficar entre 1 e 300 segundos")
        return values

    @field_validator("discovery_allowlist")
    @classmethod
    def validate_allowlist(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            network = ip_network(value.strip(), strict=False)
            if not network.is_private or network.version != 4 or network.num_addresses > 256:
                raise ValueError("Discovery aceita somente redes IPv4 privadas de até /24")
            normalized.append(str(network))
        return normalized


class SentinelTokenUpdate(BaseModel):
    token: str = Field(min_length=32, max_length=512)


class SentinelDebugRequest(BaseModel):
    severity: SentinelSeverity


class SentinelHistoryQuery(BaseModel):
    hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=50, ge=1, le=200)
    severity: str = Field(default="", pattern=r"^(|info|warning|critical|recovery)$")


class SentinelSearchQuery(SentinelHistoryQuery):
    query: str = Field(min_length=1, max_length=120)
