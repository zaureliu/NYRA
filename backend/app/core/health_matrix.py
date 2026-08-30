"""Consolidated subsystem health matrix (hardening spec §11-§15, §218-§219).

Normalizes every subsystem into the shared state vocabulary:

    DISABLED / UNCONFIGURED / STARTING / READY / DEGRADED / FAILED /
    OFFLINE / RECOVERING / STALE

READY must mean READY (§13): dependencies are taken into account, timestamps
are mandatory (§29 stale detection), and a failing optional integration never
breaks the report itself (§16 failure isolation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from app.core.paths import DATA_ROOT, PROJECT_ROOT

logger = logging.getLogger("nyra.health")

STALE_AFTER_SECONDS = 180.0
WATCHDOG_STALE_SECONDS = 30.0


class SubsystemState(str, Enum):
    DISABLED = "DISABLED"
    UNCONFIGURED = "UNCONFIGURED"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"
    RECOVERING = "RECOVERING"
    STALE = "STALE"


CORE_SUBSYSTEMS = ("api", "llm", "memory")


@dataclass
class SubsystemHealth:
    name: str
    enabled: bool = True
    configured: bool = True
    state: SubsystemState = SubsystemState.UNCONFIGURED
    healthy: bool | None = None
    last_error: str | None = None
    last_success: str | None = None
    dependencies: list[str] = field(default_factory=list)
    recovery_available: bool = False
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "configured": self.configured,
            "state": self.state.value,
            "healthy": self.healthy,
            "last_error": self.last_error,
            "last_success": self.last_success,
            "dependencies": list(self.dependencies),
            "recovery_available": self.recovery_available,
            "observed_at": self.observed_at,
            "details": self.details,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: float | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _error_text(value: Any, limit: int = 200) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:limit]


def _state_from_bool(healthy: bool, *, degraded_value: bool | None = None) -> SubsystemState:
    if healthy:
        return SubsystemState.READY
    if degraded_value is True:
        return SubsystemState.DEGRADED
    return SubsystemState.FAILED


# ------------------------------------------------------------------ collectors


def _collect_api(services) -> SubsystemHealth:
    return SubsystemHealth(name="api", state=SubsystemState.READY, healthy=True)


async def _collect_llm(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="llm", dependencies=["ollama"], recovery_available=True)
    brain = services.brain or services.llm
    entry.details["provider"] = getattr(brain, "name", "unknown")
    entry.details["official_model"] = getattr(brain, "official_model", None)
    entry.details["active_model"] = getattr(brain, "model", None)
    fallback = getattr(brain, "last_fallback", None)
    if fallback:
        entry.details["last_fallback"] = fallback
    try:
        healthy, ready = await asyncio.gather(brain.health(), brain.ready())
    except Exception as error:  # noqa: BLE001 - isolation per collector (§16)
        entry.state, entry.healthy = SubsystemState.OFFLINE, False
        entry.last_error = _error_text(error)
        return entry
    entry.healthy = healthy
    entry.details["ready"] = ready
    if not healthy:
        entry.state = SubsystemState.OFFLINE
        return entry
    warm_status = services.warm_manager.status() if services.warm_manager else None
    if warm_status:
        entry.details["warm_state"] = warm_status.get("state")
        entry.last_error = _error_text(warm_status.get("last_error"))
        entry.details["observed_at_warm"] = _iso(warm_status.get("observed_at")) \
            if isinstance(warm_status.get("observed_at"), (int, float)) else warm_status.get("observed_at")
    model_resident = bool(warm_status and warm_status.get("state") == "OLLAMA_READY") or ready is True
    if healthy and model_resident:
        entry.state = SubsystemState.READY
    elif healthy:
        # process/API alive but model not resident yet is STARTING, not READY (§13/§14)
        entry.state = SubsystemState.STARTING if (warm_status and warm_status.get("state") == "OLLAMA_LOADING") \
            else SubsystemState.DEGRADED
        if entry.state is SubsystemState.STARTING:
            entry.details["note"] = "modelo carregando"
    return entry


async def _collect_ollama(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="ollama", dependencies=[], recovery_available=True)
    status = services.warm_manager.status() if services.warm_manager else None
    settings_model = services.settings.llm_model
    entry.details["configured_model"] = settings_model
    if not status:
        entry.configured = False
        entry.state = SubsystemState.UNCONFIGURED
        return entry
    state = str(status.get("state") or "")
    entry.details["warm_state"] = state
    entry.details["metrics"] = status.get("metrics")
    entry.last_error = _error_text(status.get("last_error"))
    mapping = {
        "OLLAMA_READY": SubsystemState.READY,
        "OLLAMA_LOADING": SubsystemState.STARTING,
        "OLLAMA_OFFLINE": SubsystemState.OFFLINE,
        "OLLAMA_ERROR": SubsystemState.FAILED,
    }
    entry.state = mapping.get(state, SubsystemState.DEGRADED)
    if state == "OLLAMA_READY":
        installed = await _model_installed(services, settings_model)
        entry.details["installed"] = installed
        if not installed:
            entry.state = SubsystemState.FAILED
            entry.last_error = f"MODEL_NOT_INSTALLED:{settings_model}"
    return entry


async def _model_installed(services, model: str) -> bool:
    del services
    import httpx

    from app.core.config import get_settings

    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.get(f"{get_settings().ollama_url}/api/tags")
            tags = response.json().get("models", [])
        return any(item.get("name") == model for item in tags)
    except Exception:  # noqa: BLE001
        return False


async def _collect_memory(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="memory", dependencies=["database"])
    try:
        healthy = await services.memory.health()
    except Exception as error:  # noqa: BLE001
        entry.state, entry.healthy = SubsystemState.FAILED, False
        entry.last_error = _error_text(error)
        return entry
    entry.healthy = healthy
    entry.state = _state_from_bool(healthy)
    return entry


async def _collect_database(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="database", dependencies=[])
    path: Path = services.settings.database_path
    entry.details["path"] = str(path)
    if not Path(path).exists():
        entry.state = SubsystemState.FAILED
        entry.last_error = "DATABASE_FILE_MISSING"
        return entry
    try:
        connection = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=3)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        result = str(row[0]) if row else "missing"
        entry.details["quick_check"] = result
        entry.state = SubsystemState.READY if result == "ok" else SubsystemState.FAILED
        if entry.state is SubsystemState.FAILED:
            entry.last_error = f"INTEGRITY:{result[:80]}"
    except sqlite3.Error as error:
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
    return entry


async def _collect_voice(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="voice", dependencies=[])
    try:
        stt_ok, tts_ok = await asyncio.gather(services.stt.health(), services.tts.health())
    except Exception as error:  # noqa: BLE001
        entry.state, entry.healthy = SubsystemState.FAILED, False
        entry.last_error = _error_text(error)
        return entry
    entry.details["stt"] = {"provider": services.stt.name, "healthy": stt_ok}
    entry.details["tts"] = {"provider": services.tts.name, "healthy": tts_ok}
    microphone = None
    try:
        listening_status = services.listening.status()
        microphone = bool(listening_status.get("microphone"))
        entry.details["microphone"] = microphone
    except Exception:  # noqa: BLE001
        pass
    entry.healthy = stt_ok and tts_ok
    if stt_ok and tts_ok:
        entry.state = SubsystemState.READY
    elif stt_ok or tts_ok:
        entry.state = SubsystemState.DEGRADED
        entry.last_error = "TTS_UNHEALTHY" if not tts_ok else "STT_UNHEALTHY"
    else:
        entry.state = SubsystemState.FAILED
    return entry


async def _collect_agent(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="agent", dependencies=["llm", "tools"])
    try:
        status = services.agent.status()
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    entry.details.update({key: status.get(key) for key in ("active_runs", "read_only_mode") if key in status})
    entry.state = SubsystemState.READY
    entry.healthy = True
    return entry


def _collect_tools(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="tools", dependencies=[])
    try:
        count = len(services.tools.descriptions())
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    entry.details["registered"] = count
    entry.state = SubsystemState.READY if count else SubsystemState.FAILED
    entry.healthy = bool(count)
    return entry


def _collect_runtime_services(services) -> SubsystemHealth:
    del services
    raise RuntimeError("use collect_runtime")


async def collect_runtime(services) -> SubsystemHealth:
    """Runtime supervisor aggregated view (used inside async context)."""
    entry = SubsystemHealth(name="runtime_services", dependencies=[], recovery_available=True)
    try:
        snapshots = await services.runtime_supervisor.inspect_all_public()
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    expected = len(snapshots)
    running = ready = failed = 0
    children = []
    for snapshot in snapshots:
        state = str(getattr(snapshot, "state", ""))
        service_id = getattr(snapshot, "service_id", "?")
        verified = getattr(snapshot, "verification_status", "")
        child = {"id": service_id, "state": state}
        if verified:
            child["verification"] = verified
        spec_disabled = state == "DISABLED"
        if not spec_disabled:
            if state == "READY":
                ready += 1
                child["ok"] = True
            elif state in {"RUNNING", "STARTING", "RESTARTING"}:
                running += 1
            elif state in {"FAILED", "CRASH_LOOP"}:
                failed += 1
                child["ok"] = False
        children.append(child)
    entry.details["services"] = children
    entry.details["counts"] = {"expected": expected, "ready": ready,
                               "running": running, "failed": failed}
    if failed == 0:
        entry.state = SubsystemState.READY if ready + running >= max(1, expected - _disabled_count(children)) - 0 \
            else SubsystemState.DEGRADED
    elif failed and ready:
        entry.state = SubsystemState.DEGRADED
    else:
        entry.state = SubsystemState.FAILED
    return entry


def _disabled_count(children: list[dict]) -> int:
    return sum(1 for item in children if item.get("state") == "DISABLED")


async def _collect_jobs(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="jobs", dependencies=[])
    operator_v2 = services.operator_v2
    jobs = getattr(operator_v2, "jobs", None) if operator_v2 else None
    if jobs is None:
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        return entry
    try:
        listing = await jobs.list(include_terminal=False)
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    items = listing.get("jobs", []) if isinstance(listing, dict) else []
    active = [item for item in items if item.get("state") in {"QUEUED", "STARTING", "RUNNING", "WAITING"}]
    failed_recent = [item for item in items if item.get("state") == "FAILED"]
    entry.details["total"] = len(items)
    entry.details["active"] = len(active)
    entry.details["failed"] = len(failed_recent)
    entry.state = SubsystemState.READY
    entry.healthy = True
    return entry


async def _collect_workflows(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="workflows", dependencies=["tools"])
    workflows = getattr(services.operator_v2, "workflows", None) if services.operator_v2 else None
    if workflows is None:
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        return entry
    try:
        listing = workflows.list_workflows()
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    entry.details["count"] = listing.get("count", 0)
    entry.state = SubsystemState.READY
    entry.healthy = True
    return entry


async def _collect_desktop(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="desktop", dependencies=[])
    controller = services.desktop
    if controller is None:
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        return entry
    try:
        windows = controller.status_windows()
        apps = controller.list_apps() if hasattr(controller, "list_apps") else {}
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    entry.details["apps_catalogued"] = len(apps.get("apps", []) if isinstance(apps, dict) else [])
    entry.details["windows"] = len(windows.get("windows", []) if isinstance(windows, dict) else [])
    entry.state = SubsystemState.READY
    entry.healthy = True
    return entry


async def _collect_watchdog(_services) -> SubsystemHealth:
    entry = SubsystemHealth(name="watchdog", dependencies=[], recovery_available=True)
    heartbeat_path = DATA_ROOT / "watchdog-heartbeat.json"
    entry.details["heartbeat_path"] = str(heartbeat_path)
    if not heartbeat_path.exists():
        # watchdog é opcional: ausência não é erro (estado válido §100)
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        entry.details["note"] = "watchdog externo não iniciado"
        return entry
    try:
        document = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    written = document.get("timestamp") or document.get("written_at")
    age = None
    if isinstance(written, (int, float)):
        age = max(0.0, _utcnow().timestamp() - float(written))
    entry.details["heartbeat_age_seconds"] = round(age, 1) if age is not None else None
    entry.details["components"] = document.get("components")
    entry.details["decisions"] = document.get("decisions")
    if age is not None and age > WATCHDOG_STALE_SECONDS * 6:
        entry.state = SubsystemState.STALE
    elif document.get("crash_loop_protected"):
        entry.state = SubsystemState.RECOVERING
    else:
        entry.state = SubsystemState.READY
    entry.healthy = entry.state in {SubsystemState.READY, SubsystemState.RECOVERING}
    return entry


async def _collect_homelab(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="homelab", dependencies=[], recovery_available=True)
    homelab = services.homelab
    if homelab is None:
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        return entry
    try:
        status = homelab.status()
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    integrations = status.get("integrations", {}) if isinstance(status, dict) else {}
    entry.details["integrations"] = {
        key: value.get("state") if isinstance(value, dict) else str(value)
        for key, value in integrations.items()
    }
    hosts = integrations.get("hosts") if isinstance(integrations.get("hosts"), dict) else {}
    configured_any = any(str(value.get("state")).upper() not in {"UNCONFIGURED", "DISABLED"}
                         for value in hosts.values()) if hosts else \
        any(str(value).upper() not in {"UNCONFIGURED", "DISABLED"} for value in entry.details["integrations"].values())
    entry.configured = bool(configured_any)
    if not entry.configured:
        entry.state = SubsystemState.UNCONFIGURED
        return entry
    degraded = any(str(value.get("state")).upper() in {"DEGRADED", "AUTHENTICATION_FAILED",
                                                      "INTEGRATION_UNAVAILABLE"}
                   for value in hosts.values()) if hosts else False
    offline = all(str(value.get("state")).upper() in {"OFFLINE", "UNREACHABLE"}
                  for value in hosts.values()) if hosts else False
    if offline:
        entry.state = SubsystemState.OFFLINE
    elif degraded:
        entry.state = SubsystemState.DEGRADED
    else:
        entry.state = SubsystemState.READY
    entry.healthy = entry.state in {SubsystemState.READY, SubsystemState.DEGRADED}
    return entry


async def _collect_integrations(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="integrations", dependencies=[])
    components: dict[str, SubsystemState] = {}
    details: dict[str, Any] = {}

    sentinel = services.sentinel
    sentinel_enabled = bool(getattr(getattr(sentinel, "settings", None), "sentinel_watch_enabled", False))
    if not sentinel_enabled:
        components["sentinel"] = SubsystemState.DISABLED
    else:
        raw = str(getattr(sentinel, "state", "")).upper()
        mapping = {
            "CONNECTED": SubsystemState.READY,
            "RECONNECTING": SubsystemState.RECOVERING,
            "AUTH_FAILED": SubsystemState.FAILED,
            "OFFLINE": SubsystemState.OFFLINE,
            "ERROR": SubsystemState.FAILED,
            "DISCOVERING": SubsystemState.STARTING,
            "FOUND": SubsystemState.STARTING,
            "AUTH_REQUIRED": SubsystemState.UNCONFIGURED,
            "INCOMPATIBLE": SubsystemState.DEGRADED,
        }
        components["sentinel"] = mapping.get(raw, SubsystemState.DEGRADED)
        details["sentinel_state"] = raw.lower()

    # prompt11_1: credenciais via resolução única (Broker → legado).
    from app.integrations.proxmox.config import resolve_credentials

    pm_token_id, pm_token_secret = resolve_credentials(services.settings)
    configured = bool(pm_token_id and pm_token_secret)
    components["proxmox"] = SubsystemState.UNCONFIGURED if not configured else SubsystemState.READY
    details["proxmox_configured"] = bool(configured)

    from app.integrations.home_assistant_profiles import (
        active_profile_id,
        resolve_profile_token,
    )

    ha_active = active_profile_id()
    ha_token = bool(resolve_profile_token(str(ha_active))) if ha_active \
        else bool(getattr(services.settings, "home_assistant_token", None))
    ha_url = bool(getattr(services.settings, "home_assistant_url", None))
    components["home_assistant"] = SubsystemState.UNCONFIGURED if not (ha_token and ha_url) \
        else SubsystemState.READY
    details["home_assistant_configured"] = bool(ha_token and ha_url)

    openwrt_password = bool(getattr(services.settings, "openwrt_password", None))
    openwrt_host = bool(getattr(services.settings, "openwrt_host", None))
    components["openwrt"] = SubsystemState.UNCONFIGURED if not (openwrt_password and openwrt_host) \
        else SubsystemState.READY
    details["openwrt_configured"] = bool(openwrt_password and openwrt_host)

    entry.details = details
    entry.details["components"] = {key: value.value for key, value in components.items()}
    states = list(components.values())
    if all(state in {SubsystemState.DISABLED, SubsystemState.UNCONFIGURED} for state in states):
        entry.configured = False
        entry.state = SubsystemState.UNCONFIGURED
    elif SubsystemState.FAILED in states:
        entry.state = SubsystemState.DEGRADED  # integração opcional degradada não derruba o núcleo (§16)
    elif SubsystemState.READY in states:
        entry.state = SubsystemState.READY
    else:
        entry.state = SubsystemState.UNCONFIGURED
    entry.healthy = entry.state in {SubsystemState.READY, SubsystemState.UNCONFIGURED}
    return entry


async def _collect_conversation(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="conversation", dependencies=["llm"])
    try:
        state = services.conversation.state.value
        registry = getattr(services.orchestrator, "turns", None)
        metrics = registry.snapshot() if registry else None
    except Exception as error:  # noqa: BLE001
        entry.state = SubsystemState.FAILED
        entry.last_error = _error_text(error)
        return entry
    entry.details["pipeline_state"] = state
    if isinstance(metrics, dict):
        entry.details["turn_metrics"] = metrics.get("metrics")
    entry.state = SubsystemState.READY
    entry.healthy = True
    return entry


async def _collect_listening(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="always_listening", dependencies=["voice"])
    if not services.listening.enabled:
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        entry.details["enabled"] = False
        return entry
    status = services.listening.status()
    entry.details["enabled"] = True
    entry.details["muted"] = bool(status.get("muted"))
    entry.details["microphone"] = bool(status.get("microphone"))
    if not status.get("microphone"):
        entry.state = SubsystemState.DEGRADED
        entry.last_error = "MICROPHONE_UNAVAILABLE"
    else:
        entry.state = SubsystemState.READY
    entry.healthy = entry.state is SubsystemState.READY
    return entry


async def _collect_usb(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="usb_monitor", dependencies=[], recovery_available=True)
    service = getattr(services, "usb", None)
    if service is None:
        entry.enabled = False
        entry.state = SubsystemState.DISABLED
        entry.healthy = True
        return entry
    snapshot = await service.status_snapshot()
    mapping = {
        "STARTING": SubsystemState.STARTING,
        "ACTIVE": SubsystemState.READY,
        "DEGRADED": SubsystemState.DEGRADED,
        "STOPPED": SubsystemState.OFFLINE,
    }
    entry.state = mapping.get(str(snapshot.get("monitor_state")), SubsystemState.DEGRADED)
    entry.healthy = entry.state in {SubsystemState.READY, SubsystemState.DEGRADED}
    entry.last_error = snapshot.get("last_error")
    entry.last_success = snapshot.get("last_heartbeat_at")
    entry.details = {
        "monitor_state": snapshot.get("monitor_state"),
        "event_source": snapshot.get("event_source"),
        "connected": snapshot.get("connected_count", 0),
        "unknown": snapshot.get("unknown_count", 0),
    }
    return entry


def _collect_vts_presence(services) -> SubsystemHealth:
    entry = SubsystemHealth(name="vts_presence", dependencies=["desktop"], recovery_available=True)
    provider = getattr(services, "vtube_studio", None)
    if provider is None:
        entry.enabled = False; entry.state = SubsystemState.DISABLED; entry.healthy = True
        return entry
    status = provider.readiness(); config = status.get("config", {}); presence = status.get("vts_presence", {})
    mode = config.get("renderer", "AUTO")
    if not config.get("enabled", True) or mode in {"INTERNAL", "CURRENT"}:
        entry.enabled = False; entry.state = SubsystemState.DISABLED; entry.healthy = True
    elif presence.get("state") == "VTS_ACTIVE" and presence.get("alpha") == "VALID":
        entry.state = SubsystemState.READY; entry.healthy = True
    else:
        entry.state = SubsystemState.DEGRADED; entry.healthy = True
        entry.last_error = presence.get("error") or status.get("last_error")
    entry.details = {
        "mode": mode,
        "api_state": status.get("state"),
        "api_port": config.get("port"),
        "model_name": status.get("model"),
        "spout_state": presence.get("state", "INTERNAL_ACTIVE"),
        "sender": presence.get("sender"),
        "width": presence.get("width", 0),
        "height": presence.get("height", 0),
        "fps": presence.get("receiver_fps", 0),
        "alpha": presence.get("alpha", "UNKNOWN"),
        "last_frame_ms": presence.get("last_frame_age_ms"),
        "renderer": "DIRECTX11_DIRECTCOMPOSITION",
        "fallback_active": presence.get("fallback_active", True),
    }
    return entry


COLLECTORS: dict[str, Callable[[Any], Any]] = {}


def _register(name: str, function) -> None:
    COLLECTORS[name] = function


_register("api", _collect_api)
_register("llm", _collect_llm)
_register("memory", _collect_memory)
_register("database", _collect_database)
_register("voice", _collect_voice)
_register("agent", _collect_agent)
_register("tools", _collect_tools)
_register("jobs", _collect_jobs)
_register("workflows", _collect_workflows)
_register("desktop", _collect_desktop)
_register("watchdog", _collect_watchdog)
_register("homelab", _collect_homelab)
_register("integrations", _collect_integrations)
_register("conversation", _collect_conversation)
_register("listening", _collect_listening)
_register("usb_monitor", _collect_usb)
_register("vts_presence", _collect_vts_presence)


SUBSYSTEM_DEPENDENCIES = {
    "api": [],
    "llm": ["ollama"],
    "ollama": [],
    "memory": ["database"],
    "database": [],
    "voice": [],
    "agent": ["llm", "tools"],
    "tools": [],
    "jobs": ["tools"],
    "workflows": ["tools"],
    "desktop": [],
    "watchdog": [],
    "homelab": [],
    "integrations": [],
    "conversation": ["llm"],
    "always_listening": ["voice"],
    "usb_monitor": [],
    "vts_presence": ["desktop"],
    "runtime_services": [],
}


async def build_health_report(services, *, include_details: bool = True) -> dict:
    """Consolidated Health Report (spec §218-§219). Never raises."""
    generated = _utcnow()
    entries: dict[str, SubsystemHealth] = {}

    async def _run(name: str, collector) -> None:
        try:
            result = collector(services)
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=20)
            entries[name] = result
        except asyncio.TimeoutError:
            entry = SubsystemHealth(name=name, state=SubsystemState.STALE,
                                    last_error=f"collector timeout ({name})")
            entries[name] = entry
        except Exception as error:  # noqa: BLE001 - isolamento por coletor (§16)
            logger.warning("health collector failed for %s: %s", name, error)
            entries[name] = SubsystemHealth(name=name, state=SubsystemState.FAILED,
                                            last_error=_error_text(error))

    tasks = [_run(name, collector) for name, collector in COLLECTORS.items()]
    tasks.append(_run("runtime_services", collect_runtime))
    tasks.append(_run("ollama", _collect_ollama))
    await asyncio.gather(*tasks)

    for name, entry in entries.items():
        entry.dependencies = SUBSYSTEM_DEPENDENCIES.get(name, entry.dependencies)
        observed = entry.observed_at
        try:
            observed_dt = datetime.fromisoformat(observed)
            if generated - observed_dt > timedelta(seconds=STALE_AFTER_SECONDS):
                entry.state = SubsystemState.STALE
        except ValueError:
            pass

    core_failed = any(entries[name].state in {SubsystemState.FAILED, SubsystemState.OFFLINE}
                      for name in CORE_SUBSYSTEMS if name in entries)
    core_degraded = any(entries[name].state in {SubsystemState.DEGRADED, SubsystemState.STALE,
                                                SubsystemState.STARTING}
                        for name in CORE_SUBSYSTEMS if name in entries)
    optional_failed = any(entry.state in {SubsystemState.FAILED, SubsystemState.OFFLINE}
                          for name, entry in entries.items() if name not in CORE_SUBSYSTEMS)
    if core_failed:
        overall = "FAILED"
    elif core_degraded:
        overall = "DEGRADED"
    elif optional_failed:
        overall = "DEGRADED"
    else:
        overall = "READY"

    summary = {
        "total": len(entries),
        "ready": sum(1 for e in entries.values() if e.state is SubsystemState.READY),
        "degraded": sum(1 for e in entries.values() if e.state is SubsystemState.DEGRADED),
        "failed": sum(1 for e in entries.values() if e.state in {SubsystemState.FAILED, SubsystemState.OFFLINE}),
        "unconfigured": sum(1 for e in entries.values() if e.state is SubsystemState.UNCONFIGURED),
        "disabled": sum(1 for e in entries.values() if e.state is SubsystemState.DISABLED),
        "stale": sum(1 for e in entries.values() if e.state is SubsystemState.STALE),
    }

    nodes = [{"id": name, "state": entry.state.value} for name, entry in sorted(entries.items())]
    edges = [
        {"from": dependency, "to": name}
        for name, entry in sorted(entries.items())
        for dependency in entry.dependencies
        if dependency in entries
    ]

    return {
        "generated_at": generated.isoformat(),
        "overall": overall,
        "summary": summary,
        "subsystems": {name: entry.as_dict() for name, entry in sorted(entries.items())},
        "graph": {"nodes": nodes, "edges": edges},
        "details_included": include_details,
        "version": 1,
    }
