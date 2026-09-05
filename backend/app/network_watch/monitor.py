from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings
from app.core.runtime_settings import save_runtime_settings
from app.events import EventBus, EventType
from app.network_watch.history import NetworkHistory
from app.network_watch.metrics import RollingNetworkMetrics
from app.network_watch.models import (
    ConnectivityTargetSnapshot,
    NetworkEvent,
    NetworkHealth,
    NetworkInterfaceSnapshot,
    NetworkQualitySnapshot,
    NetworkSeverity,
    NetworkSnapshot,
    NetworkWatchSettingsUpdate,
)
from app.network_watch.rules import NetworkRuleEngine
from app.network_watch.targets import (
    DefaultRoute,
    calculate_counter_rates,
    detect_default_route,
    dns_probe,
    http_probe,
    icmp_probe,
    interface_counters,
    tcp_probe,
)


logger = logging.getLogger("kazumi.network_watch")
EVENT_TYPES = {
    "network_monitor_started": EventType.NETWORK_MONITOR_STARTED,
    "network_monitor_stopped": EventType.NETWORK_MONITOR_STOPPED,
    "gateway_down": EventType.NETWORK_GATEWAY_DOWN,
    "gateway_recovered": EventType.NETWORK_GATEWAY_RECOVERED,
    "internet_down": EventType.NETWORK_INTERNET_DOWN,
    "internet_recovered": EventType.NETWORK_INTERNET_RECOVERED,
    "dns_failure": EventType.NETWORK_DNS_FAILURE,
    "dns_recovered": EventType.NETWORK_DNS_RECOVERED,
    "high_latency": EventType.NETWORK_HIGH_LATENCY,
    "very_high_latency": EventType.NETWORK_HIGH_LATENCY,
    "latency_recovered": EventType.NETWORK_LATENCY_RECOVERED,
    "packet_loss": EventType.NETWORK_PACKET_LOSS,
    "high_packet_loss": EventType.NETWORK_PACKET_LOSS,
    "packet_loss_recovered": EventType.NETWORK_PACKET_LOSS_RECOVERED,
    "high_jitter": EventType.NETWORK_HIGH_JITTER,
    "jitter_recovered": EventType.NETWORK_JITTER_RECOVERED,
    "interface_down": EventType.NETWORK_LINK_DOWN,
    "link_up": EventType.NETWORK_LINK_UP,
    "interface_changed": EventType.NETWORK_INTERFACE_CHANGED,
    "network_recovered": EventType.NETWORK_RECOVERED,
    "rx_errors_detected": EventType.NETWORK_RX_ERRORS_DETECTED,
    "tx_errors_detected": EventType.NETWORK_TX_ERRORS_DETECTED,
    "drops_detected": EventType.NETWORK_DROPS_DETECTED,
    "quiet_mode_enabled": EventType.NETWORK_QUIET_MODE_CHANGED,
    "quiet_mode_disabled": EventType.NETWORK_QUIET_MODE_CHANGED,
}


class NetworkWatchMonitor:
    """Read-only, bounded, event-driven network observability collector."""

    def __init__(self, settings: Settings, event_bus: EventBus) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.enabled = settings.network_watch_enabled
        self.started_at: datetime | None = None
        self.snapshot = NetworkSnapshot()
        self.samples: deque[NetworkSnapshot] = deque(maxlen=900)
        self.metrics = RollingNetworkMetrics(max_samples=900)
        self.rules = NetworkRuleEngine(settings)
        self.history = NetworkHistory(settings.database_path, settings.network_history_retention_days)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._route = DefaultRoute(None, None, None)
        self._previous_interface: tuple[str | None, str | None, str | None] | None = None
        self._previous_counters: tuple[float, dict[str, Any]] | None = None
        self._due: dict[str, float] = {}
        self._targets: dict[str, dict[str, Any]] = {}
        self._counter_event_at: dict[str, float] = {}

    async def initialize(self) -> None:
        await self.history.initialize()
        await self.history.cleanup()
        if self.enabled:
            self.start()

    def start(self) -> None:
        self.enabled = True
        if self._task is None or self._task.done():
            self._stop.clear()
            self.started_at = datetime.now(timezone.utc)
            self._due.clear()
            self._task = asyncio.create_task(self._run(), name="kazumi-network-watch")
            asyncio.create_task(
                self._emit(NetworkEvent(
                    type="network_monitor_started",
                    severity=NetworkSeverity.INFO,
                    message="Monitoramento de rede iniciado.",
                )),
                name="kazumi-network-watch-start-event",
            )

    async def stop(self) -> None:
        was_running = bool(self._task and not self._task.done())
        self.enabled = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if was_running:
            await self._emit(NetworkEvent(
                type="network_monitor_stopped",
                severity=NetworkSeverity.INFO,
                message="Monitoramento de rede encerrado.",
            ))
        logger.info("network_watch_stopped")

    def config(self) -> NetworkWatchSettingsUpdate:
        targets = [item.strip() for item in self.settings.network_internet_targets.split(",") if item.strip()]
        return NetworkWatchSettingsUpdate(
            enabled=self.enabled,
            voice_alerts=self.settings.network_voice_alerts,
            desktop_alerts=self.settings.network_desktop_alerts,
            quiet_mode=self.settings.network_quiet_mode,
            critical_voice_in_quiet=self.settings.network_critical_voice_in_quiet,
            interface_interval=self.settings.network_interface_interval,
            gateway_interval=self.settings.network_gateway_interval,
            internet_interval=self.settings.network_internet_interval,
            dns_interval=self.settings.network_dns_interval,
            http_interval=self.settings.network_http_interval,
            latency_warning_ms=self.settings.network_latency_warning_ms,
            latency_critical_ms=self.settings.network_latency_critical_ms,
            packet_loss_warning=self.settings.network_packet_loss_warning,
            packet_loss_critical=self.settings.network_packet_loss_critical,
            jitter_warning_ms=self.settings.network_jitter_warning_ms,
            alert_cooldown_seconds=self.settings.network_alert_cooldown_seconds,
            history_retention_days=self.settings.network_history_retention_days,
            dns_target=self.settings.network_dns_target,
            internet_targets=targets,
        )

    async def update(self, value: NetworkWatchSettingsUpdate) -> dict[str, Any]:
        was_enabled = self.enabled
        was_quiet = self.settings.network_quiet_mode
        updates = {
            "network_watch_enabled": value.enabled,
            "network_voice_alerts": value.voice_alerts,
            "network_desktop_alerts": value.desktop_alerts,
            "network_quiet_mode": value.quiet_mode,
            "network_critical_voice_in_quiet": value.critical_voice_in_quiet,
            "network_interface_interval": value.interface_interval,
            "network_gateway_interval": value.gateway_interval,
            "network_internet_interval": value.internet_interval,
            "network_dns_interval": value.dns_interval,
            "network_http_interval": value.http_interval,
            "network_latency_warning_ms": value.latency_warning_ms,
            "network_latency_critical_ms": value.latency_critical_ms,
            "network_packet_loss_warning": value.packet_loss_warning,
            "network_packet_loss_critical": value.packet_loss_critical,
            "network_jitter_warning_ms": value.jitter_warning_ms,
            "network_alert_cooldown_seconds": value.alert_cooldown_seconds,
            "network_history_retention_days": value.history_retention_days,
            "network_dns_target": value.dns_target,
            "network_internet_targets": ",".join(value.internet_targets),
        }
        for key, item in updates.items():
            setattr(self.settings, key, item)
        self.history.retention_days = value.history_retention_days
        self.rules = NetworkRuleEngine(self.settings)
        await asyncio.to_thread(save_runtime_settings, updates)
        if value.enabled and not was_enabled:
            self.start()
        elif not value.enabled and was_enabled:
            await self.stop()
        self.enabled = value.enabled
        if was_quiet != value.quiet_mode:
            enabled = value.quiet_mode
            await self._emit(NetworkEvent(
                type="quiet_mode_enabled" if enabled else "quiet_mode_disabled",
                severity=NetworkSeverity.NOTICE,
                message=f"Modo silencioso {'ativado' if enabled else 'desativado'}.",
            ))
        await self.event_bus.publish(EventType.NETWORK_STATUS_UPDATED, **self.status())
        return self.status()

    async def poll_once(self, force: bool = True) -> dict[str, Any]:
        if not self.enabled and not force:
            return self.status()
        now = time.monotonic()
        await self._poll_interface(now, force)
        await asyncio.gather(
            self._poll_gateway(now, force),
            self._poll_internet(now, force),
            self._poll_dns(now, force),
            self._poll_http(now, force),
        )
        summary = self.metrics.summary(window=30)
        self.snapshot.latency_average_ms = summary["average_ms"]
        self.snapshot.latency_min_ms = summary["min_ms"]
        self.snapshot.latency_max_ms = summary["max_ms"]
        self.snapshot.jitter_ms = summary["jitter_ms"]
        self.snapshot.packet_loss_percent = summary["packet_loss_percent"]
        self.snapshot.timestamp = datetime.now(timezone.utc)
        self._sync_v2_snapshot()
        self.samples.append(self.snapshot.model_copy(deep=True))
        for event in self.rules.evaluate(self.snapshot):
            await self._emit(event)
        await self.event_bus.publish(EventType.NETWORK_STATUS_UPDATED, **self.status())
        return self.status()

    async def inject(self, key: str) -> NetworkEvent:
        event = self.rules.inject(key, self.snapshot)
        event.simulated = True
        await self._emit(event)
        return event

    def status(self) -> dict[str, Any]:
        running = bool(self._task and not self._task.done())
        self.snapshot.health = self._health()
        overall = {
            NetworkHealth.DISABLED: "disabled",
            NetworkHealth.COLLECTING: "starting",
            NetworkHealth.HEALTHY: "online",
            NetworkHealth.WARNING: "degraded",
            NetworkHealth.DEGRADED: "degraded",
            NetworkHealth.CRITICAL: "offline",
        }[self.snapshot.health]
        uptime = (datetime.now(timezone.utc) - self.started_at).total_seconds() if self.started_at else 0
        return {
            "schema_version": 2,
            "enabled": self.enabled,
            "running": running,
            "status": overall,
            "health": self.snapshot.health.value,
            "uptime_seconds": round(uptime, 1),
            "snapshot": self.snapshot.model_dump(mode="json"),
            "active_alerts": sorted(self.rules._active),
            "voice_alerts": self.settings.network_voice_alerts,
            "quiet_mode": self.settings.network_quiet_mode,
            "history_limit": self.samples.maxlen,
        }

    def sample_window(self, minutes: int = 5, since: datetime | None = None) -> list[dict[str, Any]]:
        count = max(1, min(900, int(minutes * 60 / max(self.settings.network_interface_interval, 0.5))))
        selected = list(self.samples)[-count:]
        if since is not None:
            normalized = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            selected = [item for item in selected if item.timestamp > normalized]
        return [item.model_dump(mode="json") for item in selected]

    async def _poll_interface(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("interface", 0)):
            return
        self._due["interface"] = now + self.settings.network_interface_interval
        if force or now >= self._due.get("route", 0):
            self._due["route"] = now + max(5, self.settings.network_gateway_interval)
            self._route = await detect_default_route()
        data = await asyncio.to_thread(interface_counters, self._route.interface_name)
        current = (self._route.gateway, data.get("name"), data.get("ip_address"))
        if self._previous_interface and current != self._previous_interface:
            previous_name = self._previous_interface[1] or "indisponível"
            current_name = current[1] or "indisponível"
            await self._emit(NetworkEvent(
                type="interface_changed",
                severity=NetworkSeverity.NOTICE,
                message=f"A interface ativa mudou de {previous_name} para {current_name}.",
                metrics={"previous": self._previous_interface, "current": current},
            ))
            self._previous_counters = None
        self._previous_interface = current
        self.snapshot.gateway = self._route.gateway
        self.snapshot.interface_name = data.get("name")
        self.snapshot.interface_up = data.get("up")
        self.snapshot.ip_address = data.get("ip_address")
        previous = self._previous_counters
        rates = calculate_counter_rates(
            previous[1] if previous else None,
            data,
            now - previous[0] if previous else 0,
        )
        self._previous_counters = (now, dict(data))
        self.snapshot.rx_bytes_per_sec = rates.rx_bytes_per_sec
        self.snapshot.tx_bytes_per_sec = rates.tx_bytes_per_sec
        self.snapshot.download_bps = rates.rx_bytes_per_sec * 8 if rates.rx_bytes_per_sec is not None else None
        self.snapshot.upload_bps = rates.tx_bytes_per_sec * 8 if rates.tx_bytes_per_sec is not None else None
        self.snapshot.rx_packets_per_sec = rates.rx_packets_per_sec
        self.snapshot.tx_packets_per_sec = rates.tx_packets_per_sec
        self.snapshot.bytes_sent = data.get("bytes_sent")
        self.snapshot.bytes_received = data.get("bytes_received")
        self.snapshot.packets_sent = data.get("packets_sent")
        self.snapshot.packets_received = data.get("packets_received")
        self.snapshot.errors_sent = data.get("errors_sent")
        self.snapshot.errors_received = data.get("errors_received")
        self.snapshot.drops_sent = data.get("drops_sent")
        self.snapshot.drops_received = data.get("drops_received")
        self.snapshot.interface = NetworkInterfaceSnapshot(
            name=data.get("name"), type=data.get("type"), ipv4=data.get("ip_address"),
            ipv6=data.get("ipv6_address"), link_up=data.get("up"),
            link_speed_mbps=data.get("link_speed_mbps"), mtu=data.get("mtu"),
            bytes_rx=data.get("bytes_received"), bytes_tx=data.get("bytes_sent"),
            packets_rx=data.get("packets_received"), packets_tx=data.get("packets_sent"),
            errors_rx=data.get("errors_received"), errors_tx=data.get("errors_sent"),
            drops_rx=data.get("drops_received"), drops_tx=data.get("drops_sent"),
            rx_bytes_per_sec=rates.rx_bytes_per_sec, tx_bytes_per_sec=rates.tx_bytes_per_sec,
            rx_packets_per_sec=rates.rx_packets_per_sec, tx_packets_per_sec=rates.tx_packets_per_sec,
        )
        self._observe_target("local_interface", data.get("name"), data.get("up"), None)
        await self._emit_counter_events(rates, now)

    async def _poll_gateway(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("gateway", 0)):
            return
        self._due["gateway"] = now + self.settings.network_gateway_interval
        if not self._route.gateway:
            self.snapshot.gateway_alive = None
            self.snapshot.gateway_latency_ms = None
            self._observe_target("gateway", None, None, None)
            return
        self.snapshot.gateway_alive, self.snapshot.gateway_latency_ms = await icmp_probe(self._route.gateway)
        self._observe_target("gateway", self._route.gateway, self.snapshot.gateway_alive, self.snapshot.gateway_latency_ms)

    async def _poll_internet(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("internet", 0)):
            return
        self._due["internet"] = now + self.settings.network_internet_interval
        targets = [item.strip() for item in self.settings.network_internet_targets.split(",") if item.strip()]
        results = await asyncio.gather(*(tcp_probe(target) for target in targets), return_exceptions=True)
        valid = [item for item in results if isinstance(item, tuple)]
        reachable = any(item[0] for item in valid)
        latencies = [item[1] for item in valid if item[0] and item[1] is not None]
        latency = round(sum(latencies) / len(latencies), 2) if latencies else None
        self.snapshot.internet_reachable = reachable
        self.snapshot.internet_latency_ms = latency
        self.metrics.add_probe(reachable, latency)
        self._observe_target("internet", "multi-probe", reachable, latency)

    async def _poll_dns(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("dns", 0)):
            return
        self._due["dns"] = now + self.settings.network_dns_interval
        self.snapshot.dns_ok, self.snapshot.dns_latency_ms = await dns_probe(self.settings.network_dns_target)
        self.snapshot.dns_sample_id += 1
        self._observe_target("dns", self.settings.network_dns_target, self.snapshot.dns_ok, self.snapshot.dns_latency_ms)

    async def _poll_http(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("http", 0)):
            return
        self._due["http"] = now + self.settings.network_http_interval
        self.snapshot.http_ok, _ = await http_probe()

    async def _emit(self, event: NetworkEvent, event_type: EventType | None = None) -> None:
        await self.history.add(event)
        selected = event_type or EVENT_TYPES.get(event.type, EventType.NETWORK_ALERT)
        await self.event_bus.publish(selected, **event.model_dump(mode="json"))
        log = logger.info if event.severity in {NetworkSeverity.INFO, NetworkSeverity.NOTICE} else logger.warning
        log("network_event type=%s severity=%s", event.type, event.severity.value)

    def _observe_target(self, kind: str, address: str | None, reachable: bool | None, latency_ms: float | None) -> None:
        now = datetime.now(timezone.utc)
        target = self._targets.setdefault(kind, {
            "outcomes": deque(maxlen=60), "reachable": None,
            "last_success_at": None, "last_failure_at": None, "last_transition_at": None,
        })
        previous = target.get("reachable")
        if reachable is not None:
            target["outcomes"].append(bool(reachable))
            target["last_success_at" if reachable else "last_failure_at"] = now
            if previous is not None and previous != reachable:
                target["last_transition_at"] = now
        target.update({"address": address, "reachable": reachable, "latency_ms": latency_ms, "last_probe_at": now})

    def _target_snapshot(self, kind: str) -> ConnectivityTargetSnapshot:
        value = self._targets.get(kind, {})
        reachable = value.get("reachable")
        outcomes = list(value.get("outcomes") or [])
        state = "unavailable" if reachable is None else ("healthy" if reachable else "down")
        return ConnectivityTargetSnapshot(
            kind=kind, address=value.get("address"), state=state, reachable=reachable,
            latency_ms=value.get("latency_ms"), last_probe_at=value.get("last_probe_at"),
            last_success_at=value.get("last_success_at"), last_failure_at=value.get("last_failure_at"),
            last_transition_at=value.get("last_transition_at"),
            recent_success_ratio=round(sum(outcomes) / len(outcomes) * 100, 1) if outcomes else None,
        )

    def _health(self) -> NetworkHealth:
        if not self.enabled:
            return NetworkHealth.DISABLED
        item = self.snapshot
        if item.interface_up is None or item.internet_reachable is None:
            return NetworkHealth.COLLECTING
        if item.interface_up is False or item.gateway_alive is False or item.internet_reachable is False:
            return NetworkHealth.CRITICAL
        if item.dns_ok is False:
            return NetworkHealth.DEGRADED
        loss, latency, jitter = item.packet_loss_percent, item.latency_average_ms, item.jitter_ms
        if (loss is not None and loss > self.settings.network_packet_loss_critical) or (
            latency is not None and latency > self.settings.network_latency_critical_ms
        ):
            return NetworkHealth.DEGRADED
        if (loss is not None and loss > self.settings.network_packet_loss_warning) or (
            latency is not None and latency > self.settings.network_latency_warning_ms
        ) or (jitter is not None and jitter > self.settings.network_jitter_warning_ms):
            return NetworkHealth.WARNING
        return NetworkHealth.HEALTHY

    def _sync_v2_snapshot(self) -> None:
        self.snapshot.health = self._health()
        self.snapshot.quality = NetworkQualitySnapshot(
            latency_ms=self.snapshot.internet_latency_ms,
            latency_average_ms=self.snapshot.latency_average_ms,
            latency_min_ms=self.snapshot.latency_min_ms,
            latency_max_ms=self.snapshot.latency_max_ms,
            jitter_ms=self.snapshot.jitter_ms,
            packet_loss_percent=self.snapshot.packet_loss_percent,
        )
        self.snapshot.local_interface = self._target_snapshot("local_interface")
        self.snapshot.gateway_state = self._target_snapshot("gateway")
        self.snapshot.dns_state = self._target_snapshot("dns")
        self.snapshot.internet_state = self._target_snapshot("internet")

    async def _emit_counter_events(self, rates, now: float) -> None:
        candidates = (
            ("rx_errors_detected", rates.errors_rx_delta, "Erros de recepção foram detectados na interface."),
            ("tx_errors_detected", rates.errors_tx_delta, "Erros de transmissão foram detectados na interface."),
            ("drops_detected", (rates.drops_rx_delta or 0) + (rates.drops_tx_delta or 0), "Descartes de pacotes foram detectados na interface."),
        )
        for key, count, message in candidates:
            if not count or now - self._counter_event_at.get(key, float("-inf")) < self.settings.network_alert_cooldown_seconds:
                continue
            self._counter_event_at[key] = now
            await self._emit(NetworkEvent(
                type=key, severity=NetworkSeverity.WARNING, message=message,
                metrics={"count": count, "interface": self.snapshot.interface_name},
            ))

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once(force=False)
            except Exception as exc:
                logger.exception("network_poll_failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except TimeoutError:
                continue
