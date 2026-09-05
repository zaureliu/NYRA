from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class NetworkSeverity(StrEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"
    RECOVERY = "recovery"


class NetworkEvent(BaseModel):
    type: str
    severity: NetworkSeverity
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    diagnosis: str | None = None
    recovered_at: datetime | None = None
    simulated: bool = False


class NetworkHealth(StrEnum):
    DISABLED = "disabled"
    COLLECTING = "collecting"
    HEALTHY = "healthy"
    WARNING = "warning"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class NetworkInterfaceSnapshot(BaseModel):
    name: str | None = None
    type: str | None = None
    ipv4: str | None = None
    ipv6: str | None = None
    link_up: bool | None = None
    link_speed_mbps: float | None = None
    mtu: int | None = None
    bytes_rx: int | None = None
    bytes_tx: int | None = None
    packets_rx: int | None = None
    packets_tx: int | None = None
    errors_rx: int | None = None
    errors_tx: int | None = None
    drops_rx: int | None = None
    drops_tx: int | None = None
    rx_bytes_per_sec: float | None = None
    tx_bytes_per_sec: float | None = None
    rx_packets_per_sec: float | None = None
    tx_packets_per_sec: float | None = None


class NetworkQualitySnapshot(BaseModel):
    latency_ms: float | None = None
    latency_average_ms: float | None = None
    latency_min_ms: float | None = None
    latency_max_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_percent: float | None = None


class ConnectivityTargetSnapshot(BaseModel):
    kind: str
    address: str | None = None
    state: str = "unavailable"
    reachable: bool | None = None
    latency_ms: float | None = None
    last_probe_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_transition_at: datetime | None = None
    recent_success_ratio: float | None = None


class NetworkSnapshot(BaseModel):
    schema_version: int = 2
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    health: NetworkHealth = NetworkHealth.COLLECTING
    gateway: str | None = None
    gateway_alive: bool | None = None
    gateway_latency_ms: float | None = None
    internet_reachable: bool | None = None
    internet_latency_ms: float | None = None
    dns_ok: bool | None = None
    dns_latency_ms: float | None = None
    dns_sample_id: int = 0
    http_ok: bool | None = None
    interface_name: str | None = None
    interface_up: bool | None = None
    ip_address: str | None = None
    bytes_sent: int | None = None
    bytes_received: int | None = None
    upload_bps: float | None = None
    download_bps: float | None = None
    rx_bytes_per_sec: float | None = None
    tx_bytes_per_sec: float | None = None
    packets_received: int | None = None
    packets_sent: int | None = None
    rx_packets_per_sec: float | None = None
    tx_packets_per_sec: float | None = None
    errors_received: int | None = None
    errors_sent: int | None = None
    drops_received: int | None = None
    drops_sent: int | None = None
    packet_loss_percent: float | None = None
    jitter_ms: float | None = None
    latency_min_ms: float | None = None
    latency_max_ms: float | None = None
    latency_average_ms: float | None = None
    interface: NetworkInterfaceSnapshot | None = None
    quality: NetworkQualitySnapshot | None = None
    local_interface: ConnectivityTargetSnapshot | None = None
    gateway_state: ConnectivityTargetSnapshot | None = None
    dns_state: ConnectivityTargetSnapshot | None = None
    internet_state: ConnectivityTargetSnapshot | None = None


class NetworkWatchSettingsUpdate(BaseModel):
    enabled: bool
    voice_alerts: bool = True
    desktop_alerts: bool = True
    quiet_mode: bool = False
    critical_voice_in_quiet: bool = False
    interface_interval: float = Field(1, ge=0.5, le=60)
    gateway_interval: float = Field(2, ge=1, le=300)
    internet_interval: float = Field(5, ge=2, le=600)
    dns_interval: float = Field(15, ge=5, le=3600)
    http_interval: float = Field(30, ge=10, le=3600)
    latency_warning_ms: float = Field(100, ge=10, le=5000)
    latency_critical_ms: float = Field(200, ge=20, le=10000)
    packet_loss_warning: float = Field(5, ge=0, le=100)
    packet_loss_critical: float = Field(15, ge=0, le=100)
    jitter_warning_ms: float = Field(40, ge=1, le=5000)
    alert_cooldown_seconds: int = Field(300, ge=10, le=86400)
    history_retention_days: int = Field(30, ge=1, le=365)
    dns_target: str = Field("cloudflare.com", min_length=1, max_length=253)
    internet_targets: list[str] = Field(default_factory=lambda: ["1.1.1.1:443", "8.8.8.8:53"], min_length=2, max_length=8)

    @field_validator("internet_targets")
    @classmethod
    def validate_targets(cls, values: list[str]) -> list[str]:
        for value in values:
            host, separator, port = value.rpartition(":")
            if not separator or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError(f"Alvo inválido: {value}")
        return values


class NetworkDebugRequest(BaseModel):
    event: str = Field(
        pattern=r"^(gateway_down|internet_down|dns_failure|high_latency|packet_loss|network_recovered)$"
    )
