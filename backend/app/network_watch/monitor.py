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
from app.network_watch.models import NetworkEvent, NetworkSnapshot, NetworkWatchSettingsUpdate
from app.network_watch.rules import NetworkRuleEngine
from app.network_watch.targets import (
    DefaultRoute,
    detect_default_route,
    dns_probe,
    http_probe,
    icmp_probe,
    interface_counters,
    tcp_probe,
)


logger = logging.getLogger("nyra.network_watch")
EVENT_TYPES = {
    "gateway_down": EventType.NETWORK_GATEWAY_DOWN,
    "internet_down": EventType.NETWORK_INTERNET_DOWN,
    "dns_failure": EventType.NETWORK_DNS_FAILURE,
    "high_latency": EventType.NETWORK_HIGH_LATENCY,
    "very_high_latency": EventType.NETWORK_HIGH_LATENCY,
    "packet_loss": EventType.NETWORK_PACKET_LOSS,
    "high_packet_loss": EventType.NETWORK_PACKET_LOSS,
    "high_jitter": EventType.NETWORK_HIGH_JITTER,
    "interface_down": EventType.NETWORK_INTERFACE_CHANGED,
    "network_recovered": EventType.NETWORK_RECOVERED,
}


class NetworkWatchMonitor:
    """Read-only, asynchronous network-quality monitor."""

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
        self._previous_counters: tuple[float, int, int] | None = None
        self._due: dict[str, float] = {}

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
            self._task = asyncio.create_task(self._run(), name="nyra-network-watch")

    async def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

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
        self.snapshot.jitter_ms = float(summary["jitter_ms"] or 0)
        self.snapshot.packet_loss_percent = float(summary["packet_loss_percent"] or 0)
        self.snapshot.timestamp = datetime.now(timezone.utc)
        self.samples.append(self.snapshot.model_copy(deep=True))
        for event in self.rules.evaluate(self.snapshot):
            await self._emit(event)
        await self.event_bus.publish(EventType.NETWORK_STATUS_UPDATED, **self.status())
        return self.status()

    async def inject(self, key: str) -> NetworkEvent:
        event = self.rules.inject(key, self.snapshot)
        await self._emit(event)
        return event

    def status(self) -> dict[str, Any]:
        running = bool(self._task and not self._task.done())
        overall = "disabled"
        if self.enabled:
            if self.snapshot.internet_reachable is False or self.snapshot.interface_up is False:
                overall = "offline"
            elif len(self.metrics.outcomes) >= 5 and (
                self.snapshot.packet_loss_percent > self.settings.network_packet_loss_warning
                or self.snapshot.jitter_ms > self.settings.network_jitter_warning_ms
            ):
                overall = "degraded"
            else:
                overall = "online" if self.snapshot.internet_reachable else "starting"
        uptime = (datetime.now(timezone.utc) - self.started_at).total_seconds() if self.started_at else 0
        return {
            "enabled": self.enabled,
            "running": running,
            "status": overall,
            "uptime_seconds": round(uptime, 1),
            "snapshot": self.snapshot.model_dump(mode="json"),
            "active_alerts": sorted(self.rules._active),
            "voice_alerts": self.settings.network_voice_alerts,
            "quiet_mode": self.settings.network_quiet_mode,
        }

    def sample_window(self, minutes: int = 1) -> list[dict[str, Any]]:
        count = max(1, min(900, int(minutes * 60 / max(self.settings.network_interface_interval, 0.5))))
        return [item.model_dump(mode="json") for item in list(self.samples)[-count:]]

    async def _poll_interface(self, now: float, force: bool) -> None:
        if force or now >= self._due.get("interface", 0):
            self._due["interface"] = now + self.settings.network_interface_interval
            if force or now >= self._due.get("route", 0):
                self._due["route"] = now + max(5, self.settings.network_gateway_interval)
                self._route = await detect_default_route()
            data = await asyncio.to_thread(interface_counters, self._route.interface_name)
            current = (self._route.gateway, data.get("name"), data.get("ip_address"))
            if self._previous_interface and current != self._previous_interface:
                event = NetworkEvent(
                    type="interface_changed",
                    severity="info",
                    message="A rota padrão ou a interface ativa mudou.",
                    metrics={"previous": self._previous_interface, "current": current},
                )
                await self._emit(event, EventType.NETWORK_INTERFACE_CHANGED)
            self._previous_interface = current
            self.snapshot.gateway = self._route.gateway
            self.snapshot.interface_name = data.get("name")
            self.snapshot.interface_up = data.get("up")
            self.snapshot.ip_address = data.get("ip_address")
            sent = data.get("bytes_sent")
            received = data.get("bytes_received")
            if self._previous_counters and sent is not None and received is not None:
                previous_time, previous_sent, previous_received = self._previous_counters
                elapsed = max(0.001, now - previous_time)
                self.snapshot.upload_bps = max(0, round((sent - previous_sent) * 8 / elapsed, 1))
                self.snapshot.download_bps = max(0, round((received - previous_received) * 8 / elapsed, 1))
            if sent is not None and received is not None:
                self._previous_counters = (now, sent, received)
            self.snapshot.bytes_sent = sent
            self.snapshot.bytes_received = received

    async def _poll_gateway(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("gateway", 0)):
            return
        self._due["gateway"] = now + self.settings.network_gateway_interval
        if not self._route.gateway:
            self.snapshot.gateway_alive = None
            self.snapshot.gateway_latency_ms = None
            return
        self.snapshot.gateway_alive, self.snapshot.gateway_latency_ms = await icmp_probe(self._route.gateway)

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

    async def _poll_dns(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("dns", 0)):
            return
        self._due["dns"] = now + self.settings.network_dns_interval
        self.snapshot.dns_ok, self.snapshot.dns_latency_ms = await dns_probe(self.settings.network_dns_target)
        self.snapshot.dns_sample_id += 1

    async def _poll_http(self, now: float, force: bool) -> None:
        if not (force or now >= self._due.get("http", 0)):
            return
        self._due["http"] = now + self.settings.network_http_interval
        self.snapshot.http_ok, _ = await http_probe()

    async def _emit(self, event: NetworkEvent, event_type: EventType | None = None) -> None:
        await self.history.add(event)
        selected = event_type or EVENT_TYPES.get(event.type, EventType.NETWORK_ALERT)
        await self.event_bus.publish(selected, **event.model_dump(mode="json"))
        logger.warning(
            "network_event",
            extra={"event_type": event.type, "severity": event.severity.value, "diagnosis": event.diagnosis},
        )

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
