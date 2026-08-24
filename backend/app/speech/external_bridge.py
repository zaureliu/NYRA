"""VoiceProcessorBridge (prompt11 Parte W, §120-§129).

Ponte para um processador de voz EXTERNO/LOCAL dedicado capaz de oferecer:

    STT · TTS · VAD · AEC · noise suppression · voice conversion · streaming

Protocolos suportados: localhost HTTP (health + capability negotiation) e
localhost WebSocket.  O endpoint default é ``http://127.0.0.1:8977`` e NUNCA
é exposto na LAN (§123): endpoints não-loopback são rejeitados.

Comportamento honesto:
* Health check obrigatório com circuit breaker (5 falhas ⇒ backoff 60s, §68/§157).
* Se o processor cair, o pipeline interno continua funcionando (fallback, §128);
  chat textual nunca depende daqui (§129).
* Nenhum áudio sai do host: conexão apenas loopback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.runtime_settings import save_runtime_settings

logger = logging.getLogger("nyra.voice_bridge")

DEFAULT_ENDPOINT = "http://127.0.0.1:8977"
PROBE_TIMEOUT_SECONDS = 2.0
BREAKER_FAILURE_THRESHOLD = 5
BREAKER_BACKOFF_SECONDS = 60.0

KNOWN_CAPABILITIES = ("stt", "tts", "vad", "aec", "ns", "streaming")


def _is_loopback_url(endpoint: str) -> bool:
    try:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            return False
        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            return False
        if hostname == "localhost":
            return True
        address = ip_address(hostname)
        return address.is_loopback
    except ValueError:
        return False


class VoiceProcessorBridge:
    """Estado + sondas do processador de voz externo."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._enabled = bool(getattr(settings, "voice_processor_bridge_enabled", False))
        self._endpoint = str(getattr(settings, "voice_processor_bridge_endpoint", "")
                             or DEFAULT_ENDPOINT)
        self._autostart = bool(getattr(settings, "voice_processor_bridge_autostart", False))
        self._protocol = str(getattr(settings, "voice_processor_bridge_protocol", "http")
                             or "http")
        self._capabilities: dict[str, bool] = {}
        self._processor_name: str = ""
        self._last_probe_at: float = 0.0
        self._latency_ms: float | None = None
        self._last_error: str = ""
        self._consecutive_failures: int = 0
        self._breaker_open_until: float = 0.0
        self._health: str = "UNKNOWN"

    # ------------------------------------------------------------------ config

    def config(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "protocol": self._protocol,
            "endpoint": self._endpoint,
            "autostart": self._autostart,
        }

    async def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = str(payload.get("endpoint", self._endpoint)).strip().rstrip("/")
        if endpoint and not _is_loopback_url(endpoint):
            raise ValueError("Endpoint deve ser localhost/loopback (nunca LAN).")
        protocol = str(payload.get("protocol", self._protocol)).strip().lower()
        if protocol not in {"http", "websocket"}:
            raise ValueError("protocolo suportado: http | websocket")
        enabled = bool(payload.get("enabled", self._enabled))
        autostart = bool(payload.get("autostart", self._autostart))

        self._endpoint = endpoint or DEFAULT_ENDPOINT
        self._protocol = protocol
        self._enabled = enabled
        self._autostart = autostart

        save_runtime_settings({
            "voice_processor_bridge_enabled": enabled,
            "voice_processor_bridge_endpoint": self._endpoint,
            "voice_processor_bridge_protocol": protocol,
            "voice_processor_bridge_autostart": autostart,
        })
        if enabled:
            await self.probe()
        else:
            self._health = "UNKNOWN"
        return self.cached_status()

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return await self.update({"enabled": enabled})

    # ------------------------------------------------------------------ probes

    def _breaker_open(self) -> bool:
        return time.time() < self._breaker_open_until

    async def probe(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._breaker_open():
            return {
                **self.cached_status(),
                "ok": False,
                "error_code": "BRIDGE_BREAKER_OPEN",
                "retry_in_seconds": round(self._breaker_open_until - time.time(), 1),
            }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self._endpoint}/health")
                response.raise_for_status()
                document = response.json() if response.content else {}
        except Exception as error:  # noqa: BLE001 - falha honesta
            self._consecutive_failures += 1
            self._last_error = f"{type(error).__name__}"
            self._latency_ms = None
            self._health = "OFFLINE" if self._enabled else "UNKNOWN"
            if self._consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
                self._breaker_open_until = time.time() + BREAKER_BACKOFF_SECONDS
            logger.info("voice_bridge_unreachable error=%s failures=%s",
                        type(error).__name__, self._consecutive_failures)
            return {**self.cached_status(), "ok": False,
                    "error_code": "BRIDGE_UNREACHABLE"}

        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._latency_ms = round((time.perf_counter() - started) * 1000, 1)
        self._last_probe_at = time.time()
        raw_capabilities = document.get("capabilities") or {}
        self._capabilities = {
            name: bool(raw_capabilities.get(name, False)) for name in KNOWN_CAPABILITIES
        }
        self._processor_name = str(document.get("name") or "external-voice-processor")[:80]
        version = str(document.get("version") or "")[:24]
        healthy = bool(document.get("healthy", True))
        self._health = "HEALTHY" if healthy else "DEGRADED"
        self._last_error = ""
        return {**self.cached_status(), "ok": True, "version": version}

    def cached_status(self) -> dict[str, Any]:
        configured = _is_loopback_url(self._endpoint)
        fallback_active = self._enabled and self._health in {"OFFLINE", "UNKNOWN"}
        return {
            "enabled": self._enabled,
            "configured": configured,
            "protocol": self._protocol,
            "endpoint": self._endpoint,
            "autostart": self._autostart,
            "health": self._health,
            "connected": self._health == "HEALTHY",
            "fallback_internal_active": fallback_active,
            "capabilities": dict(self._capabilities),
            "processor_name": self._processor_name,
            "latency_ms": self._latency_ms,
            "last_probe_at": self._last_probe_at or None,
            "last_error": self._last_error or None,
            "consecutive_failures": self._consecutive_failures,
            "breaker_open": self._breaker_open(),
        }

    async def test(self) -> dict[str, Any]:
        result = await self.probe(force=True)
        return result


async def run_self_test_cycle(bridge: VoiceProcessorBridge) -> dict[str, Any]:
    """Ciclo usado pelos testes E2E: probe saudável → queda → fallback."""
    healthy = await bridge.test()
    return {"probe": healthy, "status": bridge.cached_status()}
