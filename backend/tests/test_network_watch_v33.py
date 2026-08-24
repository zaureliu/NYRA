from datetime import datetime, timezone

from app.core.config import Settings
from app.network_watch.metrics import RollingNetworkMetrics
from app.network_watch.models import NetworkSnapshot
from app.network_watch.rules import NetworkRuleEngine


def snapshot(**values) -> NetworkSnapshot:
    base = dict(
        gateway="192.168.1.1", gateway_alive=True, internet_reachable=True,
        dns_ok=True, interface_name="Ethernet", interface_up=True,
        latency_average_ms=20, packet_loss_percent=0, jitter_ms=2,
    )
    base.update(values)
    return NetworkSnapshot(**base)


def test_rolling_metrics_distinguishes_loss_and_jitter():
    metrics = RollingNetworkMetrics()
    for reachable, latency in [(True, 10), (True, 20), (False, None), (True, 15)]:
        metrics.add_probe(reachable, latency)
    result = metrics.summary()
    assert result["packet_loss_percent"] == 25
    assert result["jitter_ms"] == 7.5


def test_gateway_down_requires_hysteresis_then_recovers(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "memory.db", network_alert_cooldown_seconds=10)
    rules = NetworkRuleEngine(settings)
    broken = snapshot(gateway_alive=False)
    assert rules.evaluate(broken, now=0) == []
    events = rules.evaluate(broken, now=5.1)
    assert len(events) == 1 and events[0].type == "gateway_down"
    recovered = rules.evaluate(snapshot(), now=9)
    assert len(recovered) == 1 and recovered[0].severity == "recovery"


def test_latency_and_packet_loss_need_sustained_condition(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "memory.db", network_alert_cooldown_seconds=10)
    rules = NetworkRuleEngine(settings)
    bad = snapshot(latency_average_ms=150, packet_loss_percent=8)
    assert rules.evaluate(bad, now=0) == []
    assert rules.evaluate(bad, now=20) == []
    kinds = {event.type for event in rules.evaluate(bad, now=31)}
    assert kinds == {"high_latency", "packet_loss"}


def test_dns_requires_three_consecutive_failures(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "memory.db", network_alert_cooldown_seconds=10)
    rules = NetworkRuleEngine(settings)
    failed = snapshot(dns_ok=False, dns_sample_id=1)
    assert rules.evaluate(failed, now=0) == []
    failed.dns_sample_id = 2
    assert rules.evaluate(failed, now=1) == []
    failed.dns_sample_id = 3
    assert rules.evaluate(failed, now=2)[0].type == "dns_failure"


def test_diagnosis_is_evidence_based(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "memory.db")
    rules = NetworkRuleEngine(settings)
    event = rules.inject("internet_down", snapshot(gateway_alive=True, internet_reachable=False))
    assert event.diagnosis == "debug_injection"
    event = rules._event("internet_down", event.severity, snapshot(gateway_alive=True, internet_reachable=False), 8)
    assert event.diagnosis == "UPSTREAM_PROBLEM"
