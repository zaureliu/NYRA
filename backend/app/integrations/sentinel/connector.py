from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any

import httpx
import socketio
from pydantic import ValidationError

from app.core.config import Settings
from app.core.runtime_settings import save_runtime_settings
from app.events import EventBus, EventType
from app.integrations.sentinel.auth import SentinelSecretStore
from app.integrations.sentinel.discovery import SentinelCandidate, SentinelDiscovery
from app.integrations.sentinel.history import SentinelHistory
from app.integrations.sentinel.models import (
    BRIDGE_VERSION, MAX_EVENT_BYTES, NAMESPACE, SentinelEvent, SentinelSettingsUpdate,
    SentinelState,
)


logger = logging.getLogger("nyra.sentinel_bridge")


class SentinelConnector:
    def __init__(self, settings: Settings, event_bus: EventBus) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.history = SentinelHistory(settings.database_path, settings.sentinel_event_retention_days)
        self.discovery = SentinelDiscovery()
        self.secrets = SentinelSecretStore(fallback=settings.sentinel_bridge_token)
        self.state = SentinelState.DISABLED if not settings.sentinel_watch_enabled else SentinelState.OFFLINE
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._client: socketio.AsyncClient | None = None
        self._candidate: SentinelCandidate | None = None
        self._connected_at: datetime | None = None
        self._last_event: SentinelEvent | None = None
        self._last_error = ""
        self._events_received = 0
        self._reconnect_count = 0
        self._seen_order: deque[str] = deque(maxlen=1000)
        self._seen: set[str] = set()
        self._manual_disconnect = False
        self._started_at = datetime.now(timezone.utc)

    async def initialize(self) -> None:
        await self.history.initialize()
        await self.history.cleanup()
        if self.settings.sentinel_watch_enabled:
            self.start()

    def start(self) -> None:
        self.settings.sentinel_watch_enabled = True
        self._manual_disconnect = False
        self._stop.clear()
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="nyra-sentinel-connector")

    async def stop(self) -> None:
        self.settings.sentinel_watch_enabled = False
        self._manual_disconnect = True
        self._stop.set()
        self._wake.set()
        await self._close_client()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._set_state(SentinelState.DISABLED)

    async def shutdown(self) -> None:
        """Stops I/O while preserving the persisted opt-in preference."""
        enabled = self.settings.sentinel_watch_enabled
        self._stop.set()
        self._wake.set()
        await self._close_client()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.settings.sentinel_watch_enabled = enabled

    def config(self) -> SentinelSettingsUpdate:
        return SentinelSettingsUpdate(
            enabled=self.settings.sentinel_watch_enabled,
            auto_discovery=self.settings.sentinel_auto_discovery,
            discovery_interval=self.settings.sentinel_discovery_interval,
            host=self.settings.sentinel_host,
            port=self.settings.sentinel_port,
            prefer_manual_host=self.settings.sentinel_prefer_manual_host,
            voice_alerts=self.settings.sentinel_voice_alerts,
            desktop_alerts=self.settings.sentinel_desktop_alerts,
            critical_only=self.settings.sentinel_critical_only,
            store_event_history=self.settings.sentinel_store_event_history,
            create_episodic_memory=self.settings.sentinel_create_episodic_memory,
            auto_reconnect=self.settings.sentinel_auto_reconnect,
            reconnect_backoff=[int(item) for item in self.settings.sentinel_reconnect_backoff.split(",") if item.strip()],
            debug_mode=self.settings.sentinel_debug_mode,
            discovery_allowlist=[item.strip() for item in self.settings.sentinel_discovery_allowlist.split(",") if item.strip()],
            event_retention_days=self.settings.sentinel_event_retention_days,
            alert_cooldown_seconds=self.settings.sentinel_alert_cooldown_seconds,
            disconnect_grace_seconds=self.settings.sentinel_disconnect_grace_seconds,
        )

    async def update(self, value: SentinelSettingsUpdate) -> dict[str, Any]:
        was_enabled = self.settings.sentinel_watch_enabled
        updates = {
            "sentinel_watch_enabled": value.enabled,
            "sentinel_auto_discovery": value.auto_discovery,
            "sentinel_discovery_interval": value.discovery_interval,
            "sentinel_host": value.host,
            "sentinel_port": value.port,
            "sentinel_prefer_manual_host": value.prefer_manual_host,
            "sentinel_voice_alerts": value.voice_alerts,
            "sentinel_desktop_alerts": value.desktop_alerts,
            "sentinel_critical_only": value.critical_only,
            "sentinel_store_event_history": value.store_event_history,
            "sentinel_create_episodic_memory": value.create_episodic_memory,
            "sentinel_auto_reconnect": value.auto_reconnect,
            "sentinel_reconnect_backoff": ",".join(map(str, value.reconnect_backoff)),
            "sentinel_debug_mode": value.debug_mode,
            "sentinel_discovery_allowlist": ",".join(value.discovery_allowlist),
            "sentinel_event_retention_days": value.event_retention_days,
            "sentinel_alert_cooldown_seconds": value.alert_cooldown_seconds,
            "sentinel_disconnect_grace_seconds": value.disconnect_grace_seconds,
        }
        for key, item in updates.items():
            setattr(self.settings, key, item)
        self.history.retention_days = value.event_retention_days
        await asyncio.to_thread(save_runtime_settings, updates)
        if value.enabled and not was_enabled:
            self.start()
        elif not value.enabled and was_enabled:
            await self.stop()
        elif value.enabled:
            await self.disconnect(reconnect=True)
            self._wake.set()
        return self.status()

    async def set_token(self, token: str) -> dict[str, Any]:
        await asyncio.to_thread(self.secrets.save, token)
        self._last_error = ""
        if self.settings.sentinel_watch_enabled:
            await self.disconnect(reconnect=True)
            self._wake.set()
        return {"configured": True, "state": self.state.value}

    async def clear_token(self) -> dict[str, Any]:
        await asyncio.to_thread(self.secrets.clear)
        await self.disconnect(reconnect=True)
        await self._set_state(SentinelState.AUTH_REQUIRED)
        return {"configured": False, "state": self.state.value}

    async def find_now(self) -> dict[str, Any]:
        self._manual_disconnect = False
        if not self.settings.sentinel_watch_enabled:
            self.start()
            await asyncio.to_thread(save_runtime_settings, {"sentinel_watch_enabled": True})
        self._wake.set()
        return self.status()

    async def disconnect(self, reconnect: bool = False) -> dict[str, Any]:
        self._manual_disconnect = not reconnect
        await self._close_client()
        await self._set_state(SentinelState.RECONNECTING if reconnect else SentinelState.OFFLINE)
        return self.status()

    async def reconnect(self) -> dict[str, Any]:
        self._manual_disconnect = False
        await self._close_client()
        if self.settings.sentinel_watch_enabled:
            self.start()
            await self._set_state(SentinelState.RECONNECTING)
            self._wake.set()
        return self.status()

    async def test_connection(self) -> dict[str, Any]:
        candidate = await self.discovery.discover(self.config())
        if not candidate:
            return {"ok": False, "state": SentinelState.OFFLINE.value}
        if not self.discovery.compatible(candidate):
            return {"ok": False, "state": SentinelState.INCOMPATIBLE.value, "fingerprint": candidate.fingerprint.model_dump(mode="json")}
        status, code = await self._authenticated_status(candidate)
        return {"ok": code == 200, "http_status": code, "fingerprint": candidate.fingerprint.model_dump(mode="json"), "status": status}

    async def clear_saved_host(self) -> dict[str, Any]:
        self.discovery.clear_last_known()
        self._candidate = None
        return self.status()

    async def inject(self, severity: str) -> dict[str, Any]:
        if not self.settings.sentinel_debug_mode:
            raise PermissionError("Debug do Sentinel Watch está desativado")
        event = SentinelEvent.model_validate({
            "schema_version": 1, "event_id": f"nyra-debug-{severity}-{time.time_ns()}",
            "source": "utamo-sentinel", "instance_id": "nyra-local-debug",
            "timestamp": datetime.now(timezone.utc), "category": "integration",
            "type": f"nyra_bridge_test_{severity}", "severity": severity,
            "title": "Teste do Sentinel", "summary": f"Evento {severity} de teste do Sentinel.",
            "entity": {"type": "integration", "name": "Utamo Sentinel"}, "metadata": {"debug": True},
        })
        await self._receive_event(event.model_dump(mode="json"))
        return event.model_dump(mode="json")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.sentinel_watch_enabled,
            "state": self.state.value,
            "host": self._candidate.base_url if self._candidate else (self.settings.sentinel_host or None),
            "instance_id": self._candidate.fingerprint.instance_id if self._candidate else None,
            "sentinel_version": self._candidate.fingerprint.sentinel_version if self._candidate else None,
            "bridge_version": self._candidate.fingerprint.api_version if self._candidate else str(BRIDGE_VERSION),
            "connected_since": self._connected_at.isoformat() if self._connected_at else None,
            "last_event": self._last_event.model_dump(mode="json") if self._last_event else None,
            "events_received": self._events_received,
            "reconnect_count": self._reconnect_count,
            "token_configured": self.secrets.configured(),
            "last_error": self._last_error,
            "uptime_seconds": round((datetime.now(timezone.utc) - self._started_at).total_seconds(), 1),
        }

    async def summary(self, hours: int = 1) -> dict[str, Any]:
        return {**await self.history.summary(hours), "connection": self.status()}

    async def explicit_command(self, text: str) -> str | None:
        lowered = text.casefold()
        if not re.search(r"\bsentinel\b", lowered):
            return None
        if re.search(r"\b(ativa|ative|ligue|inicie)\b.*\b(busca|sentinel watch|sentinel)\b", lowered):
            self.start()
            await asyncio.to_thread(save_runtime_settings, {"sentinel_watch_enabled": True})
            return "Sentinel Watch ativado. Vou procurar agora."
        if re.search(r"\b(para|pare|desativa|desative|desligue)\b.*\b(procurar|busca|sentinel watch|sentinel)\b", lowered):
            await self.stop()
            await asyncio.to_thread(save_runtime_settings, {"sentinel_watch_enabled": False})
            return "Sentinel Watch desativado. A busca e a conexão foram encerradas."
        return None

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if not self.settings.sentinel_watch_enabled:
                await self._set_state(SentinelState.DISABLED)
                await self._wait(3600)
                continue
            if self._manual_disconnect:
                await self._set_state(SentinelState.OFFLINE)
                await self._wait(3600)
                continue
            await self._set_state(SentinelState.DISCOVERING if not self._candidate else SentinelState.RECONNECTING)
            candidate = await self.discovery.discover(self.config())
            if not candidate:
                self._candidate = None
                await self._set_state(SentinelState.OFFLINE)
                await self._wait(self.settings.sentinel_discovery_interval)
                continue
            self._candidate = candidate
            await self._set_state(SentinelState.FOUND)
            if not self.discovery.compatible(candidate):
                self._last_error = f"Bridge v{candidate.fingerprint.api_version} incompatível"
                await self._set_state(SentinelState.INCOMPATIBLE)
                await self._wait(self.settings.sentinel_discovery_interval)
                continue
            if candidate.fingerprint.status != "online":
                self._last_error = "Bridge encontrada, mas desativada no Sentinel"
                await self._set_state(SentinelState.OFFLINE)
                await self._wait(self.settings.sentinel_discovery_interval)
                continue
            if not self.secrets.configured():
                await self._set_state(SentinelState.AUTH_REQUIRED)
                await self._wait(self.settings.sentinel_discovery_interval)
                continue
            await self._set_state(SentinelState.CONNECTING)
            status, status_code = await self._authenticated_status(candidate)
            if status_code == 401:
                self._last_error = "Token da bridge rejeitado"
                await self._set_state(SentinelState.AUTH_FAILED)
                await self._wait(self.settings.sentinel_discovery_interval)
                continue
            if status_code != 200:
                self._last_error = f"Status da bridge retornou HTTP {status_code}"
                await self._set_state(SentinelState.RECONNECTING)
                await self._wait(self._backoff(attempt))
                attempt += 1
                continue
            try:
                await self._connect(candidate)
                attempt = 0
                if self._client:
                    await self._client.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("sentinel_connection_failed", extra={"error_type": type(exc).__name__})
            finally:
                await self._close_client()
            if not self.settings.sentinel_watch_enabled:
                continue
            if not self.settings.sentinel_auto_reconnect or self._manual_disconnect:
                await self._set_state(SentinelState.OFFLINE)
                await self._wait(self.settings.sentinel_discovery_interval)
                continue
            self._reconnect_count += 1
            await self._set_state(SentinelState.RECONNECTING)
            await self._wait(self._backoff(attempt))
            attempt += 1

    async def _connect(self, candidate: SentinelCandidate) -> None:
        client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

        @client.on("sentinel_event", namespace=NAMESPACE)
        async def on_event(payload):
            await self._receive_event(payload)

        @client.on("bridge_pong", namespace=NAMESPACE)
        async def on_pong(_payload):
            return None

        self._client = client
        await client.connect(
            candidate.base_url,
            auth={"token": self.secrets.load()},
            namespaces=[NAMESPACE],
            # Sentinel currently runs Flask-SocketIO in Werkzeug/threading mode.
            # Engine.IO polling is the stable transport there and still delivers
            # pushed Socket.IO events without discovery polling.
            transports=["polling"],
            wait_timeout=8,
        )
        self._connected_at = datetime.now(timezone.utc)
        self._last_error = ""
        await self._replay(candidate)
        await self._set_state(SentinelState.CONNECTED)

    async def _authenticated_status(self, candidate: SentinelCandidate) -> tuple[dict[str, Any], int]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{candidate.base_url}/api/integrations/nyra/status",
                    headers={"Authorization": f"Bearer {self.secrets.load()}"},
                )
            return (response.json() if response.content else {}, response.status_code)
        except (httpx.HTTPError, ValueError):
            return {}, 0

    async def _replay(self, candidate: SentinelCandidate) -> None:
        since = self._last_event.timestamp.isoformat() if self._last_event else ""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{candidate.base_url}/api/integrations/nyra/alerts/recent",
                    params={"limit": 100, "since": since},
                    headers={"Authorization": f"Bearer {self.secrets.load()}"},
                )
            if response.status_code == 200 and len(response.content) <= 512 * 1024:
                events = response.json().get("events", [])
                for item in reversed(events):
                    await self._receive_event(item, replay=True)
        except (httpx.HTTPError, ValueError, TypeError):
            logger.warning("sentinel_replay_unavailable")

    async def _receive_event(self, payload: Any, replay: bool = False) -> None:
        try:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(raw) > MAX_EVENT_BYTES:
                raise ValueError("event_too_large")
            event = SentinelEvent.model_validate(payload)
        except (ValidationError, ValueError, TypeError):
            logger.warning("sentinel_event_invalid")
            return
        if event.event_id in self._seen:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen.discard(self._seen_order[0])
        self._seen_order.append(event.event_id)
        self._seen.add(event.event_id)
        stored = True
        if self.settings.sentinel_store_event_history:
            stored = await self.history.add(event)
        if not stored:
            return
        self._last_event = event
        self._events_received += 1
        await self.event_bus.publish(EventType.SENTINEL_EVENT, event=event.model_dump(mode="json"), replay=replay)

    async def _set_state(self, state: SentinelState) -> None:
        changed = state != self.state
        previous = self.state
        self.state = state
        if changed:
            await self.event_bus.publish(
                EventType.SENTINEL_STATUS_CHANGED,
                previous=previous.value, state=state.value, status=self.status(),
            )

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        self._connected_at = None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _wait(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.1, seconds))
        except asyncio.TimeoutError:
            pass

    def _backoff(self, attempt: int) -> int:
        values = self.config().reconnect_backoff
        return values[min(attempt, len(values) - 1)]
