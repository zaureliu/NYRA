from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.events import EventBus, EventType
from app.network_watch.metrics import RollingNetworkMetrics
from app.network_watch.models import NetworkEvent, NetworkSeverity, NetworkSnapshot
from app.network_watch.monitor import NetworkWatchMonitor
from app.network_watch.rules import NetworkRuleEngine
from app.network_watch.targets import calculate_counter_rates


def counters(name="Ethernet", *, rx=1000, tx=500, prx=20, ptx=10, erx=0, etx=0, drx=0, dtx=0):
    return {
        "name": name, "bytes_received": rx, "bytes_sent": tx,
        "packets_received": prx, "packets_sent": ptx,
        "errors_received": erx, "errors_sent": etx,
        "drops_received": drx, "drops_sent": dtx,
    }


def test_counter_deltas_cover_bytes_packets_and_error_signals():
    rates = calculate_counter_rates(
        counters(), counters(rx=3000, tx=1500, prx=60, ptx=30, erx=2, etx=1, drx=3), 2,
    )
    assert rates.rx_bytes_per_sec == 1000
    assert rates.tx_bytes_per_sec == 500
    assert rates.rx_packets_per_sec == 20
    assert rates.tx_packets_per_sec == 10
    assert (rates.errors_rx_delta, rates.errors_tx_delta, rates.drops_rx_delta) == (2, 1, 3)


@pytest.mark.parametrize("current", [
    counters(rx=100, tx=50),
    counters(name="Wi-Fi", rx=3000, tx=1500),
])
def test_counter_reset_or_interface_switch_never_generates_negative_rates(current):
    rates = calculate_counter_rates(counters(rx=2000, tx=1000), current, 1)
    assert rates.rx_bytes_per_sec is None
    assert rates.tx_bytes_per_sec is None
    assert rates.rx_packets_per_sec is None


def test_metrics_use_unavailable_until_real_probe_exists():
    assert RollingNetworkMetrics().summary() == {
        "average_ms": None, "min_ms": None, "max_ms": None,
        "jitter_ms": None, "packet_loss_percent": None,
    }


def test_history_window_has_explicit_900_sample_cap(tmp_path):
    monitor = NetworkWatchMonitor(
        Settings.from_sources(database_path=tmp_path / "network.db", network_interface_interval=1),
        EventBus(),
    )
    base = datetime.now(timezone.utc)
    for index in range(950):
        monitor.samples.append(NetworkSnapshot(timestamp=base + timedelta(seconds=index)))
    assert len(monitor.sample_window(15)) == 900
    assert len(monitor.samples) == 900
    assert monitor.sample_window(15, since=base + timedelta(seconds=945))[0]["timestamp"].endswith("Z")
    assert len(monitor.sample_window(15, since=base + timedelta(seconds=945))) == 4


def test_structured_snapshot_and_deterministic_health(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "network.db", network_watch_enabled=True)
    monitor = NetworkWatchMonitor(settings, EventBus())
    monitor.enabled = True
    monitor.snapshot.interface_up = True
    monitor.snapshot.internet_reachable = True
    monitor.snapshot.gateway_alive = True
    monitor.snapshot.dns_ok = True
    monitor.snapshot.internet_latency_ms = 18.2
    monitor.snapshot.latency_average_ms = 20
    monitor.snapshot.jitter_ms = 3
    monitor.snapshot.packet_loss_percent = 0
    monitor._sync_v2_snapshot()
    payload = monitor.status()
    assert payload["schema_version"] == 2
    assert payload["health"] == "healthy"
    assert payload["snapshot"]["quality"]["latency_ms"] == 18.2
    monitor.snapshot.dns_ok = False
    assert monitor._health() == "degraded"


def test_network_rules_deduplicate_and_emit_typed_recovery(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "network.db", network_alert_cooldown_seconds=10)
    rules = NetworkRuleEngine(settings)
    broken = NetworkSnapshot(
        gateway="192.168.1.1", gateway_alive=False, interface_name="Ethernet",
        interface_up=True, internet_reachable=True, dns_ok=True,
    )
    assert rules.evaluate(broken, now=0) == []
    assert [event.type for event in rules.evaluate(broken, now=5.1)] == ["gateway_down"]
    assert rules.evaluate(broken, now=6) == []
    recovered = broken.model_copy(update={"gateway_alive": True})
    assert [event.type for event in rules.evaluate(recovered, now=8)] == ["gateway_recovered"]


@pytest.mark.asyncio
async def test_simulated_event_is_explicit_and_monitor_stops(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "network.db", network_watch_enabled=False)
    bus = EventBus()
    monitor = NetworkWatchMonitor(settings, bus)
    await monitor.history.initialize()
    event = await monitor.inject("high_latency")
    assert event.simulated is True
    assert (await monitor.history.recent(limit=1))[0]["simulated"] is True
    monitor.start()
    await monitor.stop()
    assert monitor._task is None
    assert any(item.type == EventType.NETWORK_MONITOR_STOPPED for item in bus.history())


def test_event_schema_preserves_human_message_without_fake_metrics():
    event = NetworkEvent(type="network_monitor_started", severity=NetworkSeverity.INFO, message="Monitoramento iniciado.")
    assert event.metrics == {}
    assert event.simulated is False
