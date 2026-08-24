from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.events import Event, EventBus, EventType
from app.integrations.sentinel.auth import SentinelSecretStore
from app.integrations.sentinel.connector import SentinelConnector
from app.integrations.sentinel.discovery import SentinelCandidate, SentinelDiscovery
from app.integrations.sentinel.models import SentinelFingerprint, SentinelSettingsUpdate, SentinelState
from app.integrations.sentinel.proactive import ProactiveSentinelAlerts
from app.integrations.sentinel.tools import register_sentinel_tools
from app.tools import create_tool_registry


def fingerprint(**updates):
    value = {
        "service": "utamo-sentinel", "integration": "nyra", "status": "online",
        "api_version": "1", "sentinel_version": "2.2.1", "instance_id": "instance-test",
        "capabilities": ["events", "recent_alerts", "health"], "authentication_required": True,
    }
    value.update(updates)
    return SentinelFingerprint.model_validate(value)


def event_payload(event_id="event-001"):
    return {
        "schema_version": 1, "event_id": event_id, "source": "utamo-sentinel",
        "instance_id": "instance-test", "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": "availability", "type": "device_offline", "severity": "warning",
        "title": "Dispositivo offline", "summary": "OpenWrt Remote Node parou de responder",
        "entity": {"type": "node", "name": "OpenWrt Remote Node"},
        "metadata": {"ip": "192.168.1.20"},
    }


def test_settings_restrict_discovery_to_small_private_ipv4_ranges():
    value = SentinelSettingsUpdate(discovery_allowlist=["192.168.1.0/24"])
    assert value.discovery_allowlist == ["192.168.1.0/24"]
    with pytest.raises(ValidationError):
        SentinelSettingsUpdate(discovery_allowlist=["8.8.8.0/24"])
    with pytest.raises(ValidationError):
        SentinelSettingsUpdate(discovery_allowlist=["10.0.0.0/16"])
    assert SentinelDiscovery._is_local_url("http://127.0.0.1:5000")
    assert not SentinelDiscovery._is_local_url("http://8.8.8.8:5000")


@pytest.mark.asyncio
async def test_discovery_known_hosts_first_and_no_lan_without_allowlist(tmp_path: Path):
    discovery = SentinelDiscovery(last_known_path=tmp_path / "last.json")
    config = SentinelSettingsUpdate(host="sentinel.lan", port=5000, discovery_allowlist=[])
    expected = SentinelCandidate("http://sentinel.lan:5000", fingerprint())
    discovery.probe = AsyncMock(side_effect=lambda url: expected if url == expected.base_url else None)
    result = await discovery.discover(config)
    assert result == expected
    assert discovery.probe.await_args_list[0].args[0] == "http://sentinel.lan:5000"


def test_secret_store_does_not_expose_or_version_token(tmp_path: Path):
    store = SentinelSecretStore(tmp_path / "secrets" / "token.txt")
    token = "x" * 48
    store.save(token)
    assert store.configured()
    assert store.load() == token
    assert store.path.parent.name == "secrets"
    store.clear()
    assert not store.configured()


@pytest.mark.asyncio
async def test_event_validation_deduplication_history_and_event_bus(tmp_path: Path):
    settings = Settings.from_sources(
        environment="test", database_path=tmp_path / "nyra.db", sentinel_watch_enabled=False,
        sentinel_store_event_history=True,
    )
    bus = EventBus()
    connector = SentinelConnector(settings, bus)
    connector.secrets = SentinelSecretStore(tmp_path / "token.txt")
    await connector.history.initialize()
    received = []

    async def subscriber(event):
        if event.type == EventType.SENTINEL_EVENT:
            received.append(event)

    await bus.subscribe(subscriber)
    await connector._receive_event(event_payload())
    await connector._receive_event(event_payload())
    await connector._receive_event({"invalid": True})
    assert len(received) == 1
    assert connector.status()["events_received"] == 1
    assert len(await connector.history.recent()) == 1


@pytest.mark.asyncio
async def test_connector_off_stops_all_io_and_persists_toggle(tmp_path: Path):
    settings = Settings.from_sources(
        environment="test", database_path=tmp_path / "nyra.db", sentinel_watch_enabled=False,
    )
    connector = SentinelConnector(settings, EventBus())
    connector.secrets = SentinelSecretStore(tmp_path / "token.txt")
    await connector.history.initialize()
    with patch("app.integrations.sentinel.connector.save_runtime_settings", return_value={}):
        result = await connector.update(connector.config().model_copy(update={"enabled": False}))
    assert result["state"] == SentinelState.DISABLED.value
    assert connector._task is None
    assert connector._client is None


def test_all_sentinel_tools_are_read_only(tmp_path: Path):
    settings = Settings.from_sources(environment="test", database_path=tmp_path / "nyra.db")
    connector = SentinelConnector(settings, EventBus())
    registry = create_tool_registry()
    register_sentinel_tools(registry, connector)
    tools = {item["name"]: item for item in registry.descriptions()}
    expected = {
        "get_sentinel_status", "get_sentinel_connection_status", "get_sentinel_recent_events",
        "get_sentinel_event_summary", "search_sentinel_events",
    }
    assert expected <= tools.keys()
    assert all(tools[name]["risk"] == "READ_ONLY" for name in expected)


@pytest.mark.asyncio
async def test_proactive_alerts_aggregate_voice_burst_but_keep_history(tmp_path: Path):
    settings = Settings.from_sources(
        environment="test", database_path=tmp_path / "nyra.db",
        sentinel_voice_alerts=True, sentinel_critical_only=False,
        sentinel_alert_cooldown_seconds=300,
    )
    bus = EventBus()
    connector = SentinelConnector(settings, bus)
    await connector.history.initialize()

    class Provider:
        name = "kokoro"

        async def health(self):
            return True

    class Queue:
        def __init__(self):
            self.calls = []

        async def synthesize(self, provider, text, state, priority):
            self.calls.append((text, state, priority))
            return tmp_path / "burst.wav"

    queue = Queue()
    state_machine = AsyncMock()
    proactive = ProactiveSentinelAlerts(
        settings, bus, state_machine, queue, lambda: Provider(), connector,
    )
    for index in range(4):
        payload = event_payload(f"burst-00{index}")
        payload["entity"] = {"type": "node", "name": f"Node {index}"}
        await proactive._process(Event(
            type=EventType.SENTINEL_EVENT,
            payload={"event": payload, "replay": False},
        ))
    await asyncio.sleep(0.8)
    assert len(queue.calls) == 1
    assert "quatro eventos em sequência" in queue.calls[0][0]
    await proactive.stop()
