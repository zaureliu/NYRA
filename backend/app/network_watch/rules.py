from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from app.core.config import Settings
from app.network_watch.models import NetworkEvent, NetworkSeverity, NetworkSnapshot


@dataclass(frozen=True)
class _Rule:
    key: str
    duration: float
    severity: NetworkSeverity
    predicate: Callable[[NetworkSnapshot], bool]


class NetworkRuleEngine:
    """Sustained-condition evaluator with hysteresis and per-event cooldown."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pending: dict[str, float] = {}
        self._active: dict[str, float] = {}
        self._last_emitted: dict[str, float] = {}
        self._dns_failures = 0
        self._last_dns_sample_id = -1

    def evaluate(self, snapshot: NetworkSnapshot, now: float | None = None) -> list[NetworkEvent]:
        current = time.monotonic() if now is None else now
        if snapshot.dns_sample_id != self._last_dns_sample_id:
            self._last_dns_sample_id = snapshot.dns_sample_id
            self._dns_failures = self._dns_failures + 1 if snapshot.dns_ok is False else 0
        rules = (
            _Rule("gateway_down", 5, NetworkSeverity.CRITICAL, lambda item: item.gateway is not None and item.gateway_alive is False),
            _Rule("internet_down", 8, NetworkSeverity.CRITICAL, lambda item: item.internet_reachable is False),
            _Rule("dns_failure", 0, NetworkSeverity.WARNING, lambda item: self._dns_failures >= 3),
            _Rule("very_high_latency", 15, NetworkSeverity.CRITICAL, lambda item: (item.latency_average_ms or 0) > self.settings.network_latency_critical_ms),
            _Rule("high_latency", 30, NetworkSeverity.WARNING, lambda item: self.settings.network_latency_warning_ms < (item.latency_average_ms or 0) <= self.settings.network_latency_critical_ms),
            _Rule("high_packet_loss", 15, NetworkSeverity.CRITICAL, lambda item: item.packet_loss_percent > self.settings.network_packet_loss_critical),
            _Rule("packet_loss", 30, NetworkSeverity.WARNING, lambda item: self.settings.network_packet_loss_warning < item.packet_loss_percent <= self.settings.network_packet_loss_critical),
            _Rule("high_jitter", 30, NetworkSeverity.WARNING, lambda item: item.jitter_ms > self.settings.network_jitter_warning_ms),
            _Rule("interface_down", 3, NetworkSeverity.CRITICAL, lambda item: item.interface_name is not None and item.interface_up is False),
        )
        events: list[NetworkEvent] = []
        for rule in rules:
            condition = rule.predicate(snapshot)
            if condition:
                since = self._pending.setdefault(rule.key, current)
                sustained = current - since
                if sustained >= rule.duration and rule.key not in self._active and self._cooldown_ready(rule.key, current):
                    self._active[rule.key] = current
                    events.append(self._event(rule.key, rule.severity, snapshot, sustained))
            else:
                self._pending.pop(rule.key, None)
                if rule.key in self._active:
                    started = self._active.pop(rule.key)
                    events.append(
                        NetworkEvent(
                            type="network_recovered",
                            severity=NetworkSeverity.RECOVERY,
                            message=self._recovery_message(rule.key, current - started),
                            duration_seconds=round(current - started, 1),
                            metrics=self._metrics(snapshot),
                            diagnosis=rule.key,
                        )
                    )
        return events

    def inject(self, key: str, snapshot: NetworkSnapshot) -> NetworkEvent:
        if key == "network_recovered":
            self._active.clear()
            self._pending.clear()
            return NetworkEvent(
                type=key,
                severity=NetworkSeverity.RECOVERY,
                message="A conexão voltou ao normal.",
                metrics=self._metrics(snapshot),
                diagnosis="debug_injection",
            )
        synthetic = snapshot.model_copy(deep=True)
        if key == "gateway_down":
            synthetic.gateway_alive = False
        elif key == "internet_down":
            synthetic.internet_reachable = False
        elif key == "dns_failure":
            synthetic.dns_ok = False
        elif key == "high_latency":
            synthetic.latency_average_ms = self.settings.network_latency_warning_ms + 50
        elif key == "packet_loss":
            synthetic.packet_loss_percent = min(100, self.settings.network_packet_loss_warning + 5)
        severity = NetworkSeverity.CRITICAL if key in {"gateway_down", "internet_down"} else NetworkSeverity.WARNING
        return self._event(key, severity, synthetic, 0, diagnosis="debug_injection")

    def _cooldown_ready(self, key: str, now: float) -> bool:
        previous = self._last_emitted.get(key, float("-inf"))
        if now - previous < self.settings.network_alert_cooldown_seconds:
            return False
        self._last_emitted[key] = now
        return True

    def _event(
        self,
        key: str,
        severity: NetworkSeverity,
        snapshot: NetworkSnapshot,
        duration: float,
        diagnosis: str | None = None,
    ) -> NetworkEvent:
        messages = {
            "gateway_down": "O gateway parou de responder.",
            "internet_down": "A conexão com a Internet foi perdida.",
            "dns_failure": "As consultas DNS estão falhando repetidamente.",
            "very_high_latency": f"A latência está muito alta, em torno de {snapshot.latency_average_ms or 0:.0f} ms.",
            "high_latency": f"A latência subiu, em torno de {snapshot.latency_average_ms or 0:.0f} ms.",
            "high_packet_loss": f"A perda de pacotes está crítica, em {snapshot.packet_loss_percent:.1f}%.",
            "packet_loss": f"Detectei perda de pacotes de {snapshot.packet_loss_percent:.1f}%.",
            "high_jitter": f"O jitter está elevado, em {snapshot.jitter_ms:.0f} ms.",
            "interface_down": "A interface de rede ativa foi desconectada.",
        }
        return NetworkEvent(
            type=key,
            severity=severity,
            message=messages[key],
            duration_seconds=round(duration, 1),
            metrics=self._metrics(snapshot),
            diagnosis=diagnosis or self._diagnose(key, snapshot),
        )

    @staticmethod
    def _diagnose(key: str, snapshot: NetworkSnapshot) -> str | None:
        if key == "interface_down":
            return "LOCAL_LINK_PROBLEM"
        if key == "gateway_down":
            return "LAN_GATEWAY_PROBLEM"
        if key == "dns_failure" and snapshot.internet_reachable:
            return "DNS_PROBLEM"
        if key == "internet_down" and snapshot.gateway_alive:
            return "UPSTREAM_PROBLEM"
        return None

    @staticmethod
    def _metrics(snapshot: NetworkSnapshot) -> dict:
        return {
            "gateway_latency_ms": snapshot.gateway_latency_ms,
            "internet_latency_ms": snapshot.internet_latency_ms,
            "latency_average_ms": snapshot.latency_average_ms,
            "packet_loss_percent": snapshot.packet_loss_percent,
            "jitter_ms": snapshot.jitter_ms,
            "dns_ok": snapshot.dns_ok,
        }

    @staticmethod
    def _recovery_message(key: str, duration: float) -> str:
        if key in {"gateway_down", "internet_down", "interface_down"}:
            return f"A conexão voltou. A indisponibilidade durou cerca de {duration:.0f} segundos."
        if key in {"high_latency", "very_high_latency"}:
            return "A latência voltou ao normal."
        if key in {"packet_loss", "high_packet_loss"}:
            return "A perda de pacotes voltou ao normal."
        if key == "dns_failure":
            return "As consultas DNS voltaram a responder."
        return "A qualidade da conexão voltou ao normal."
