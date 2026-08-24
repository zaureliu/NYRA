"""Feature Control Center (prompt11 Parte H, §31-§37).

Registro central de capabilities com toggle REAL:

    UI toggle → API → Settings Service → runtime → health verification → UI

Cada capability declara a chave de settings que a controla, o consumer de
runtime e se o efeito é imediato (hot) ou exige restart.  Toggles não-hot
ficam marcados como ``restart_required`` até o processo reiniciar; após o
restart os valores persistidos em ``data/settings-v33.json`` são recarregados
por ``Settings.from_sources`` e a marcação desaparece naturalmente.

Nenhum valor sensível passa por aqui; o módulo apenas reflete flags booleanas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core.runtime_settings import save_runtime_settings

logger = logging.getLogger("nyra.capabilities")


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    name: str
    category: str  # operations | desktop | voice | autonomy | homelab | integrations | network
    description: str
    setting_key: str | None  # None => capability derivada (não-toggleable)
    hot_reload: bool = False
    consumer: str = ""


SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("local_operator", "Local Operator", "operations",
                   "Operação local assistida (Operator v1 + browser tools).",
                   "local_operator_enabled", consumer="bootstrap main.py"),
    CapabilitySpec("desktop_control", "Desktop Control", "desktop",
                   "Controle de aplicações desktop via Operator V2.",
                   "desktop_ui_automation_enabled", consumer="operator.v2 desktop tools"),
    CapabilitySpec("ui_automation", "UI Automation", "desktop",
                   "Automação de interface (teclado/mouse fallback).",
                   "desktop_input_fallback_enabled", consumer="operator.v2 input automation"),
    CapabilitySpec("vision", "Vision", "operations",
                   "Captura e análise de tela/janela.",
                   "vision_enabled", consumer="operator.v2 vision engine"),
    CapabilitySpec("browser_control", "Browser Control", "operations",
                   "Controle de navegador gerenciado (CDP).",
                   "browser_control_enabled", consumer="operator.browser_v2"),
    CapabilitySpec("persistent_jobs", "Persistent Jobs", "operations",
                   "Jobs persistentes com pause/resume/cancel.",
                   "persistent_jobs_enabled", consumer="operator.jobs"),
    CapabilitySpec("task_planner", "Task Planner", "autonomy",
                   "Planejamento declarativo de tarefas com verificação.",
                   None, consumer="operator.tasks"),
    CapabilitySpec("workflow_engine", "Workflow Engine", "operations",
                   "Workflows com dry-run, preflight e rollback.",
                   "workflow_engine_enabled", consumer="operator.workflows"),
    CapabilitySpec("recovery_engine", "Recovery Engine", "autonomy",
                   "Recuperação automática de falhas de passos/runs.",
                   None, consumer="operator.recovery"),
    CapabilitySpec("desktop_watcher", "Desktop Watcher", "desktop",
                   "Observação de eventos do desktop com TTL.",
                   "desktop_watcher_enabled", consumer="operator.watcher"),
    CapabilitySpec("watchdog", "Watchdog", "operations",
                   "Supervisão externa via heartbeat file.",
                   "watchdog_enabled", consumer="watchdog/ externo"),
    CapabilitySpec("proactive_operator", "Proactive Operator", "autonomy",
                   "Iniciativas proativas por regras (default OFF).",
                   "proactive_operator_enabled", consumer="operator.proactive_rules"),
    CapabilitySpec("network_watch", "Network Watch", "network",
                   "Monitoramento de latência/jitter/loss com alertas.",
                   "network_watch_enabled", hot_reload=True,
                   consumer="network_watch.monitor"),
    CapabilitySpec("homelab_control_plane", "Homelab Control Plane", "homelab",
                   "Visão read-only do homelab (registry + probes).",
                   "homelab_enabled", hot_reload=True, consumer="homelab.controller"),
    CapabilitySpec("home_assistant", "Home Assistant", "integrations",
                   "Integração REST com Home Assistant (profiles).",
                   "home_assistant_enabled", hot_reload=True,
                   consumer="integrations.home_assistant"),
    CapabilitySpec("proxmox", "Proxmox", "integrations",
                   "Leitura read-only de nodes/VMs Proxmox.",
                   "proxmox_enabled", consumer="integrations.proxmox.client"),
    CapabilitySpec("openwrt", "OpenWrt", "integrations",
                   "Status do roteador via adapter SSH homologado.",
                   None, consumer="homelab.adapters.openwrt"),
    CapabilitySpec("sentinel", "UTAMO Sentinel", "integrations",
                   "Percepção de rede via bridge Socket.IO Sentinel.",
                   "sentinel_watch_enabled", hot_reload=True,
                   consumer="integrations.sentinel.connector"),
    CapabilitySpec("voice_engine", "Voice Engine", "voice",
                   "Pipeline de conversa falada (STT→LLM→TTS).",
                   "conversation_engine", consumer="conversation.engine"),
    CapabilitySpec("always_listening", "Always Listening", "voice",
                   "Escuta contínua com VAD local e lease exclusivo.",
                   "always_listening_enabled", hot_reload=True,
                   consumer="listening.manager"),
    CapabilitySpec("external_voice_processor", "External Voice Processor", "voice",
                   "Bridge para processador de voz externo/local (STT/TTS/VAD/AEC).",
                   None, hot_reload=True, consumer="speech.external_bridge"),
    CapabilitySpec("desktop_presence", "Desktop Presence", "desktop",
                   "Presença flutuante na área de trabalho (janela Tauri).",
                   None, consumer="tauri presence window"),
    CapabilitySpec("agent_loop", "Agent Loop", "autonomy",
                   "Loop agêntico com grounding, locks e limites.",
                   "agent_enabled", consumer="agent.controller"),
    CapabilitySpec("system_shell", "System Shell", "operations",
                   "Shell local com classificação de risco e approval.",
                   "shell_enabled", consumer="tools.system_shell"),
    CapabilitySpec("remote_shell", "Remote Shell", "operations",
                   "SSH apenas para hosts do Trusted Host Registry.",
                   "remote_shell_enabled", consumer="tools.remote_shell"),
    CapabilitySpec("runtime_supervisor", "Runtime Supervisor", "operations",
                   "Supervisão de serviços gerenciados com restart limitado.",
                   "runtime_supervisor_enabled", consumer="runtime.supervisor"),
)

_SPEC_BY_ID = {spec.id: spec for spec in SPECS}

# Marcas de restart pendente vivem só na memória: no boot as flags já são
# recarregadas dos valores persistidos, logo nada fica pendente indevidamente.
_pending_restart: set[str] = set()


def capability_definitions() -> list[dict[str, Any]]:
    return [
        {
            "id": spec.id,
            "name": spec.name,
            "category": spec.category,
            "description": spec.description,
            "consumer": spec.consumer,
            "toggleable": spec.setting_key is not None,
            "setting_key": spec.setting_key,
            "hot_reload": spec.hot_reload,
        }
        for spec in SPECS
    ]


def _probe_error(error: Exception) -> dict[str, Any]:
    return {
        "state": "DEGRADED",
        "health": "DEGRADED",
        "last_error": type(error).__name__,
        "configured": True,
    }


# ---------------------------------------------------------------------------
# Probes de runtime — baratos, sem rede externa (exceto cache local).
# Cada probe retorna {state, health, last_error, configured}.
# ---------------------------------------------------------------------------

def _operator_probe(flag: str) -> Callable[[Any], dict[str, Any]]:
    def probe(services: Any) -> dict[str, Any]:
        v2 = getattr(services, "operator_v2", None)
        if v2 is None:
            return {"state": "DISABLED", "health": "DISABLED",
                    "last_error": None, "configured": False}
        flags = v2.status().get("flags", {})
        enabled = bool(flags.get(flag))
        return {
            "state": "READY" if enabled else "DISABLED",
            "health": "READY" if enabled else "DISABLED",
            "last_error": None,
            "configured": True,
        }
    return probe


def _watchdog_probe(services: Any) -> dict[str, Any]:
    import json as _json

    settings = services.settings
    path = settings.watchdog_heartbeat_path
    if not bool(getattr(settings, "watchdog_enabled", True)):
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": False}
    if not path.exists():
        # Watchdog habilitado mas sem heartbeat: o processo externo não roda.
        return {"state": "OFFLINE", "health": "OFFLINE",
                "last_error": "SEM_HEARTBEAT_EXTERNO", "configured": True}
    try:
        document = _json.loads(path.read_text(encoding="utf-8"))
        age = max(0.0, time.time() - float(document.get("timestamp", 0)))
        state = "READY" if age <= 30 else "STALE"
        return {"state": state, "health": state, "last_error": None,
                "configured": True}
    except (OSError, ValueError):
        return {"state": "FAILED", "health": "FAILED",
                "last_error": "HEARTBEAT_UNREADABLE", "configured": True}


async def _sentinel_probe(services: Any) -> dict[str, Any]:
    status = services.sentinel.status()
    state = str(status.get("state") or "OFFLINE")
    mapped = {
        "CONNECTED": ("READY", "READY"),
        "DISCOVERING": ("STARTING", "STARTING"),
        "CONNECTING": ("STARTING", "STARTING"),
        "RECONNECTING": ("RECOVERING", "RECOVERING"),
        "AUTH_REQUIRED": ("UNCONFIGURED", "UNCONFIGURED"),
        "AUTH_FAILED": ("FAILED", "FAILED"),
        "INCOMPATIBLE": ("FAILED", "FAILED"),
        "ERROR": ("FAILED", "FAILED"),
        "DISABLED": ("DISABLED", "DISABLED"),
    }.get(state, ("OFFLINE" if state == "OFFLINE" else "DEGRADED",
                  "OFFLINE" if state == "OFFLINE" else "DEGRADED"))
    return {"state": mapped[0], "health": mapped[1],
            "last_error": status.get("last_error") or None,
            "configured": bool(status.get("token_configured"))}


def _listening_probe(services: Any) -> dict[str, Any]:
    try:
        status = services.listening.status()
    except Exception as error:  # noqa: BLE001
        return _probe_error(error)
    if not status.get("enabled"):
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": True}
    if not status.get("microphone"):
        return {"state": "DEGRADED", "health": "DEGRADED",
                "last_error": "MICROPHONE_UNAVAILABLE", "configured": True}
    return {"state": "READY", "health": "READY", "last_error": None,
            "configured": True}


def _network_probe(services: Any) -> dict[str, Any]:
    try:
        status = services.network_watch.status()
    except Exception as error:  # noqa: BLE001
        return _probe_error(error)
    if not status.get("enabled"):
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": True}
    running = bool(status.get("running", status.get("active")))
    return {"state": "READY" if running else "STARTING",
            "health": "READY" if running else "STARTING",
            "last_error": status.get("last_error") or None, "configured": True}


def _voice_engine_probe(services: Any) -> dict[str, Any]:
    try:
        conversation_state = str(services.conversation.state.value)
    except Exception as error:  # noqa: BLE001
        return _probe_error(error)
    tts_name = getattr(getattr(services, "tts", None), "name", "")
    degraded = not tts_name or tts_name == "disabled"
    return {"state": "READY" if not degraded else "DEGRADED",
            "health": "READY" if not degraded else "DEGRADED",
            "last_error": None if not degraded else "TTS_INDISPONIVEL",
            "configured": True}


def _external_bridge_probe(services: Any) -> dict[str, Any]:
    bridge = getattr(services, "voice_bridge", None)
    if bridge is None:
        app_state = getattr(services, "_app_state_ref", None)
        bridge = getattr(app_state, "voice_bridge", None) if app_state else None
    if bridge is None:
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": False}
    snapshot = bridge.cached_status()
    if not snapshot.get("enabled"):
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": snapshot.get("configured", False)}
    health = str(snapshot.get("health") or "UNKNOWN")
    mapping = {
        "HEALTHY": ("READY", "READY"),
        "DEGRADED": ("DEGRADED", "DEGRADED"),
        "FALLBACK": ("DEGRADED", "FALLBACK_INTERNO"),
        "OFFLINE": ("OFFLINE", "OFFLINE"),
    }
    state, label = mapping.get(health, ("UNCONFIGURED", "UNCONFIGURED"))
    return {"state": state, "health": label,
            "last_error": snapshot.get("last_error") or None,
            "configured": snapshot.get("configured", False)}


async def _homelab_probe(services: Any) -> dict[str, Any]:
    settings = services.settings
    if not settings.homelab_enabled:
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": True}
    try:
        overview = await asyncio.wait_for(
            services.homelab.overview(force=False), timeout=8
        )
    except Exception as error:  # noqa: BLE001
        return _probe_error(error)
    summary = getattr(overview, "summary", {}) or {}
    hosts = getattr(overview, "hosts", []) or []
    total_hosts = len(hosts)
    # Closure §28: mensagem agregada nunca contradiz hosts individuais.
    # Estados que provam alcance: ONLINE/AUTHENTICATION_FAILED/OFFLINE(de host
    # up) contam como alcançáveis; UNREACHABLE/DISABLED não.
    reachable_states = {"ONLINE", "AUTHENTICATION_FAILED", "OFFLINE", "DEGRADED"}
    reachable = sum(
        int(summary.get(state, 0)) for state in reachable_states if isinstance(summary.get(state), int)
    )
    disabled = int(summary.get("DISABLED", 0) or 0)
    active_hosts = max(0, total_hosts - disabled)
    if active_hosts == 0:
        return {"state": "READY", "health": "sem hosts ativos",
                "last_error": None, "configured": True}
    if reachable == 0:
        return {"state": "DEGRADED", "health": f"hosts indisponíveis (0/{active_hosts} alcançáveis)",
                "last_error": "HOMELAB_HOSTS_UNREACHABLE", "configured": True}
    if reachable < active_hosts:
        return {"state": "DEGRADED",
                "health": f"Alguns componentes indisponíveis ({reachable}/{active_hosts} alcançáveis)",
                "last_error": None, "configured": True}
    return {"state": "READY", "health": f"{reachable}/{active_hosts} hosts OK",
            "last_error": None, "configured": True}


def _settings_only_probe(setting_key: str) -> Callable[[Any], dict[str, Any]]:
    def probe(services: Any) -> dict[str, Any]:
        enabled = bool(getattr(services.settings, setting_key, False))
        return {"state": "READY" if enabled else "DISABLED",
                "health": "READY" if enabled else "DISABLED",
                "last_error": None,
                "configured": True}
    return probe


def _ha_probe(services: Any) -> dict[str, Any]:
    settings = services.settings
    client = getattr(getattr(services, "homelab", None), "home_assistant", None)
    if not settings.home_assistant_enabled:
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": True}
    if not settings.home_assistant_url:
        return {"state": "UNCONFIGURED", "health": "UNCONFIGURED",
                "last_error": "URL_NÃO_CONFIGURADA", "configured": False}
    if client is not None and getattr(client, "auth_missing", False):
        return {"state": "UNCONFIGURED", "health": "TOKEN_AUSENTE",
                "last_error": "HA_AUTH_MISSING", "configured": True}
    return {"state": "READY", "health": "READY", "last_error": None,
            "configured": True}


def _proxmox_probe(services: Any) -> dict[str, Any]:
    settings = services.settings
    client = getattr(services, "proxmox", None)
    if not settings.proxmox_enabled:
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": True}
    configured = bool(client.configured) if client is not None else False
    if not configured:
        return {"state": "UNCONFIGURED", "health": "UNCONFIGURED",
                "last_error": "TOKEN_NÃO_CONFIGURADO", "configured": False}
    return {"state": "READY", "health": "READY", "last_error": None,
            "configured": True}


async def _openwrt_probe(services: Any) -> dict[str, Any]:
    settings = services.settings
    remote_shell_on = bool(settings.remote_shell_enabled)
    if not settings.homelab_enabled:
        return {"state": "DISABLED", "health": "DISABLED", "last_error": None,
                "configured": True}
    try:
        overview = await asyncio.wait_for(
            services.homelab.overview(force=False), timeout=8
        )
    except Exception as error:  # noqa: BLE001
        return _probe_error(error)
    host = next(
        (h for h in (getattr(overview, "hosts", None) or [])
         if getattr(h, "host_id", "") == "openwrt"),
        None,
    )
    if host is None:
        return {"state": "UNCONFIGURED", "health": "UNCONFIGURED",
                "last_error": "HOST_FORA_DO_REGISTRY", "configured": False}
    state_value = str(getattr(host, "overall_state", ""))
    if not remote_shell_on and state_value in {"HEALTHY", "READY"}:
        return {"state": "DEGRADED", "health": "SSH_DESLIGADO",
                "last_error": None, "configured": True}
    mapping = {"HEALTHY": "READY", "READY": "READY"}
    state = mapping.get(state_value, "OFFLINE" if state_value in {"UNREACHABLE", "OFFLINE"} else "DEGRADED")
    return {"state": state, "health": state, "last_error":
            getattr(host, "integration_error_code", None), "configured": True}


def _derived_ready(consumer_present: bool, unconfigured_when_absent: bool = True):
    def probe(services: Any) -> dict[str, Any]:
        if consumer_present:
            return {"state": "READY", "health": "READY", "last_error": None,
                    "configured": True}
        return {"state": "DISABLED" if unconfigured_when_absent else "READY",
                "health": "DISABLED" if unconfigured_when_absent else "READY",
                "last_error": None, "configured": not unconfigured_when_absent}
    return probe


PROBES: dict[str, Callable[[Any], Any]] = {
    "local_operator": _settings_only_probe("local_operator_enabled"),
    "desktop_control": _settings_only_probe("desktop_ui_automation_enabled"),
    "ui_automation": _settings_only_probe("desktop_input_fallback_enabled"),
    "vision": _operator_probe("vision"),
    "browser_control": _operator_probe("browser_control"),
    "persistent_jobs": _operator_probe("persistent_jobs"),
    "task_planner": _derived_ready(True),
    "workflow_engine": _operator_probe("workflow_engine"),
    "recovery_engine": _derived_ready(True),
    "desktop_watcher": _operator_probe("desktop_watcher"),
    "watchdog": _watchdog_probe,
    "proactive_operator": _operator_probe("proactive_operator"),
    "network_watch": _network_probe,
    "homelab_control_plane": _homelab_probe,
    "home_assistant": _ha_probe,
    "proxmox": _proxmox_probe,
    "openwrt": _openwrt_probe,
    "sentinel": _sentinel_probe,
    "voice_engine": _voice_engine_probe,
    "always_listening": _listening_probe,
    "external_voice_processor": _external_bridge_probe,
    "desktop_presence": _derived_ready(True),
    "agent_loop": _settings_only_probe("agent_enabled"),
    "system_shell": _settings_only_probe("shell_enabled"),
    "remote_shell": _settings_only_probe("remote_shell_enabled"),
    "runtime_supervisor": _settings_only_probe("runtime_supervisor_enabled"),
}

# Sondas que exigem await.
ASYNC_PROBES = {
    "sentinel": _sentinel_probe,
    "homelab_control_plane": _homelab_probe,
    "openwrt": _openwrt_probe,
}


async def run_probe(spec_id: str, services: Any) -> dict[str, Any]:
    probe = PROBES.get(spec_id)
    if probe is None:
        return {"state": "UNCONFIGURED", "health": "UNKNOWN",
                "last_error": None, "configured": False}
    try:
        result = probe(services)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=10)
        return result
    except Exception as error:  # noqa: BLE001 - isolamento por sonda
        return _probe_error(error)


def _desired_enabled(spec: CapabilitySpec, services: Any) -> bool | None:
    if spec.setting_key is None:
        return None
    return bool(getattr(services.settings, spec.setting_key, False))


async def get_capabilities(services: Any) -> dict[str, Any]:
    capabilities = []
    for spec in SPECS:
        runtime = await run_probe(spec.id, services)
        desired = _desired_enabled(spec, services)
        restart_required = (
            spec.id in _pending_restart and not spec.hot_reload
        )
        capabilities.append({
            "id": spec.id,
            "name": spec.name,
            "category": spec.category,
            "description": spec.description,
            "consumer": spec.consumer,
            "toggleable": spec.setting_key is not None,
            "enabled": desired if desired is not None else runtime["state"] not in {"DISABLED"},
            "runtime_state": runtime["state"],
            "health": runtime["health"],
            "last_error": runtime["last_error"],
            "configured": runtime["configured"],
            "restart_required": restart_required,
            "hot_reload": spec.hot_reload,
        })
    summary = {
        "total": len(capabilities),
        "enabled": sum(1 for c in capabilities if c["enabled"]),
        "disabled": sum(1 for c in capabilities if not c["enabled"]),
        "degraded": sum(1 for c in capabilities if c["runtime_state"] == "DEGRADED"),
        "failed": sum(1 for c in capabilities if c["runtime_state"] == "FAILED"),
        "unconfigured": sum(1 for c in capabilities if c["runtime_state"] == "UNCONFIGURED"),
        "restart_required": sum(1 for c in capabilities if c["restart_required"]),
    }
    return {"capabilities": capabilities, "summary": summary}


# ---------------------------------------------------------------------------
# Hooks de aplicação imediata (hot reload).
# ---------------------------------------------------------------------------

async def _apply_listening(services: Any, enabled: bool) -> None:
    await services.listening.set_enabled(enabled)


async def _apply_network(services: Any, enabled: bool) -> None:
    monitor = services.network_watch
    setattr(services.settings, "network_watch_enabled", enabled)
    if enabled:
        monitor.start()
    else:
        await monitor.stop()


async def _apply_sentinel(services: Any, enabled: bool) -> None:
    connector = services.sentinel
    config = connector.config().model_copy(update={"enabled": enabled})
    await connector.update(config)


async def _apply_home_assistant(services: Any, enabled: bool) -> None:
    # ha_status() consulta settings.home_assistant_enabled a cada chamada.
    setattr(services.settings, "home_assistant_enabled", enabled)


async def _apply_homelab(services: Any, enabled: bool) -> None:
    setattr(services.settings, "homelab_enabled", enabled)


async def _apply_external_voice_processor(services: Any, enabled: bool) -> None:
    bridge = getattr(services, "voice_bridge", None)
    if bridge is None:
        raise RuntimeError("Voice bridge não inicializado")
    await bridge.set_enabled(enabled)


HOT_HOOKS: dict[str, Callable[[Any, bool], Awaitable[None]]] = {
    "always_listening": _apply_listening,
    "network_watch": _apply_network,
    "sentinel": _apply_sentinel,
    "home_assistant": _apply_home_assistant,
    "homelab_control_plane": _apply_homelab,
    "external_voice_processor": _apply_external_voice_processor,
}


async def set_capability(services: Any, capability_id: str, enabled: bool) -> dict[str, Any]:
    spec = _SPEC_BY_ID.get(capability_id)
    if spec is None:
        raise KeyError(capability_id)
    if spec.setting_key is None:
        raise PermissionError(f"{capability_id} não é toggleable")
    if not isinstance(enabled, bool):
        raise ValueError("enabled deve ser booleano")

    previous = _desired_enabled(spec, services)
    if previous == enabled and capability_id not in _pending_restart:
        runtime = await run_probe(capability_id, services)
        return _toggle_response(spec, enabled, runtime, already=True)

    # 1) persistência (Settings Service é a autoridade; sobrevive a restart)
    save_runtime_settings({spec.setting_key: enabled})
    # 2) runtime imediato no objeto de settings
    setattr(services.settings, spec.setting_key, enabled)

    verification: dict[str, Any] | None = None
    hook = HOT_HOOKS.get(capability_id)
    if spec.hot_reload and hook is not None:
        try:
            await hook(services, enabled)
            _pending_restart.discard(capability_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("capability_apply_failed id=%s error=%s",
                           capability_id, type(error).__name__)
            # rollback honesto
            setattr(services.settings, spec.setting_key, bool(previous))
            save_runtime_settings({spec.setting_key: bool(previous)})
            raise RuntimeError(f"Falha ao aplicar {spec.name}: {type(error).__name__}") from error
    elif not spec.hot_reload and previous != enabled:
        _pending_restart.add(capability_id)

    runtime = await run_probe(capability_id, services)
    response = _toggle_response(spec, enabled, runtime, already=False)
    response["verification"] = {
        "applied_immediately": bool(spec.hot_reload and hook is not None),
        "runtime_matches_desired": (
            runtime["state"] != "DISABLED"
        ) == enabled or not spec.hot_reload,
        "checked_at": time.time(),
    }
    return response


def _toggle_response(spec: CapabilitySpec, enabled: bool,
                     runtime: dict[str, Any], *, already: bool) -> dict[str, Any]:
    return {
        "id": spec.id,
        "name": spec.name,
        "enabled": enabled,
        "runtime_state": runtime["state"],
        "health": runtime["health"],
        "last_error": runtime["last_error"],
        "restart_required": spec.id in _pending_restart and not spec.hot_reload,
        "hot_reload": spec.hot_reload,
        "unchanged": already,
    }


def pending_restarts() -> list[str]:
    return sorted(_pending_restart)


def clear_pending_restart(capability_id: str) -> None:
    _pending_restart.discard(capability_id)
