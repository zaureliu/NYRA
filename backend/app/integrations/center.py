"""Integration Center V3 (prompt11 Parte K/L, §49-§73).

Agrega o estado REAL das quatro integrações suportadas em cartões únicos:

    UTAMO Sentinel · Home Assistant · Proxmox · OpenWrt

Cada cartão expõe: enabled / configured / connected / health / latency /
last_sync / last_error.  Ações (test/enable/disable/reconnect/diagnostics)
delegam para os serviços existentes — nada é duplicado ou inventado.

Sentinel offline nunca derruba o agregado: toda sonda é isolada.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("kazumi.integrations.center")

INTEGRATION_IDS = ("sentinel", "home_assistant", "proxmox", "openwrt")


def _state_from_sentinel(raw_state: str) -> str:
    return {
        "CONNECTED": "READY",
        "DISCOVERING": "STARTING",
        "CONNECTING": "STARTING",
        "RECONNECTING": "RECOVERING",
        "AUTH_REQUIRED": "UNCONFIGURED",
        "AUTH_FAILED": "FAILED",
        "INCOMPATIBLE": "FAILED",
        "ERROR": "FAILED",
        "DISABLED": "DISABLED",
        "OFFLINE": "OFFLINE",
    }.get(raw_state, "DEGRADED")


async def _safe(coro, *, default=None):
    try:
        return await asyncio.wait_for(coro, timeout=10)
    except Exception as error:  # noqa: BLE001 - isolamento por integração
        logger.info("integration_probe_failed error=%s", type(error).__name__)
        return default if default is not None else {"_error": type(error).__name__}


async def _wrap_proxmox_status(fn, services):
    """public_status é sync; empacota para o isolamento uniforme."""
    return fn(services)


async def integrations_status(services: Any) -> dict[str, Any]:
    settings = services.settings
    cards: dict[str, Any] = {}

    # ------------------------------------------------------------- sentinel
    sentinel_raw = await _safe(_sentinel_card(services), default={
        "enabled": bool(settings.sentinel_watch_enabled),
        "configured": False, "connected": False,
        "state": "OFFLINE" if settings.sentinel_watch_enabled else "DISABLED",
        "health": "OFFLINE" if settings.sentinel_watch_enabled else "DISABLED",
        "latency_ms": None, "last_sync": None, "last_error": "SONDA_INDISPONÍVEL",
    })
    cards["sentinel"] = sentinel_raw

    # -------------------------------------------------------- home assistant
    # Fonte única (§44/§45): mesmo snapshot usado pela página Homelab e pelos
    # profiles — impossível divergir Homelab=ONLINE vs Integrations=AUTH_FAILED.
    from app.integrations.home_assistant_profiles import unified_ha_state

    ha = await _safe(unified_ha_state(services), default={
        "enabled": bool(settings.home_assistant_enabled),
        "configured": False, "auth_configured": False, "authenticated": False,
        "state": "UNCONFIGURED", "health": "SNAPSHOT_INDISPONÍVEL",
        "latency_ms": None, "last_test": None, "last_success": None,
        "last_error": "SNAPSHOT_INDISPONÍVEL", "active_profile": None,
        "core_version": None, "api_state": None, "entity_count": None,
        "open_url": None,
    })
    cards["home_assistant"] = {
        "id": "home_assistant",
        "name": "Home Assistant",
        "enabled": bool(ha.get("enabled")),
        "configured": bool(ha.get("configured")),
        "auth_configured": bool(ha.get("auth_configured")),
        "authentication": "CONFIGURADA" if ha.get("auth_configured") else "AUSENTE",
        "connected": ha.get("state") == "READY",
        "state": ha.get("state"),
        "health": ha.get("health"),
        "latency_ms": ha.get("latency_ms"),
        "core_version": ha.get("core_version"),
        "api_state": ha.get("api_state"),
        "entity_count": ha.get("entity_count"),
        "last_sync": ha.get("last_test"),
        "last_test": ha.get("last_test"),
        "last_success": ha.get("last_success"),
        "last_error": ha.get("last_error"),
        "active_profile": ha.get("active_profile"),
        "open_url": ha.get("open_url"),
        "realtime_events": "NOT AVAILABLE",
    }

    # --------------------------------------------------------------- proxmox
    # Mesma fonte do formulário de configuração e da página Homelab (§44/§46):
    # sem token o estado é UNCONFIGURED; 401 vira AUTH_FAILED; TLS vira TLS_ERROR.
    from app.integrations.proxmox.config import public_status as proxmox_public_status

    pm = await _safe(_wrap_proxmox_status(proxmox_public_status, services), default={})
    if not pm:
        pm = {
            "enabled": bool(getattr(settings, "proxmox_enabled", False)),
            "configured": False, "auth_configured": False,
            "state": "UNCONFIGURED", "health": "STATUS_INDISPONÍVEL",
        }
    cards["proxmox"] = {
        "id": "proxmox",
        "name": "Proxmox",
        "enabled": bool(pm.get("enabled")),
        "configured": bool(pm.get("configured")),
        "auth_configured": bool(pm.get("auth_configured")),
        "authentication": "CONFIGURADA" if pm.get("auth_configured") else "AUSENTE",
        "connected": pm.get("state") == "READY",
        "state": pm.get("state"),
        "health": pm.get("health"),
        "latency_ms": pm.get("latency_ms"),
        "version": pm.get("version"),
        "node_count": pm.get("node_count"),
        "qemu_count": pm.get("qemu_count"),
        "lxc_count": pm.get("lxc_count"),
        "storage_count": pm.get("storage_count"),
        "last_sync": pm.get("last_test"),
        "last_test": pm.get("last_test"),
        "last_success": pm.get("last_success"),
        "last_error": pm.get("last_error"),
        "open_url": pm.get("open_url"),
    }

    # ---------------------------------------------------------------- openwrt
    openwrt_card = await _safe(_openwrt_card(services), default={
        "id": "openwrt", "name": "OpenWrt",
        "enabled": bool(settings.homelab_enabled),
        "configured": False, "connected": False,
        "state": "UNCONFIGURED", "health": "HOST_FORA_DO_REGISTRY",
        "latency_ms": None, "last_sync": None,
        "last_error": "SONDA_INDISPONÍVEL",
    })
    cards["openwrt"] = openwrt_card

    summary = {
        "total": len(cards),
        "ready": sum(1 for c in cards.values() if c.get("state") == "READY"),
        "unconfigured": sum(1 for c in cards.values() if c.get("state") == "UNCONFIGURED"),
        "disabled": sum(1 for c in cards.values() if c.get("state") == "DISABLED"),
        "failing": sum(1 for c in cards.values()
                       if c.get("state") in {"FAILED", "OFFLINE", "AUTH_FAILED", "TLS_ERROR"}),
    }
    return {"generated_at": time.time(), "integrations": cards, "summary": summary}


async def _sentinel_card(services: Any) -> dict[str, Any]:
    status = services.sentinel.status()
    state = _state_from_sentinel(str(status.get("state")))
    last_event = status.get("last_event") or {}
    return {
        "id": "sentinel",
        "name": "UTAMO Sentinel",
        "enabled": bool(status.get("enabled")),
        "configured": bool(status.get("token_configured")),
        "connected": state == "READY",
        "state": state,
        "health": state,
        "latency_ms": None,
        "last_sync": last_event.get("timestamp"),
        "last_error": status.get("last_error") or None,
        "bridge_version": status.get("bridge_version"),
        "sentinel_version": status.get("sentinel_version"),
        "events_received": status.get("events_received"),
        "host": status.get("host"),
    }


async def _openwrt_card(services: Any) -> dict[str, Any]:
    overview = await services.homelab.overview(force=False)
    host = next(
        (h for h in (getattr(overview, "hosts", None) or [])
         if getattr(h, "host_id", "") == "openwrt"),
        None,
    )
    enabled = bool(services.settings.homelab_enabled)
    if host is None:
        return {
            "id": "openwrt", "name": "OpenWrt", "enabled": enabled,
            "configured": False, "connected": False, "state": "UNCONFIGURED",
            "health": "HOST_FORA_DO_REGISTRY", "latency_ms": None,
            "last_sync": None, "last_error": "HOST_NÃO_ENCONTRADO_NO_REGISTRY",
        }
    icmp_latency = None
    reachable = bool(getattr(host, "reachable", False))
    for probe in getattr(host, "probes", []) or []:
        if getattr(probe, "kind", "") == "icmp" and probe.success:
            icmp_latency = probe.latency_ms
            break
    overall = str(getattr(host, "overall_state", ""))
    integration_error = getattr(host, "integration_error_code", None)
    # §91: SSH auth failed NÃO vira Offline quando o ping funciona.
    # HealthState saudável é ONLINE (models.py) — "HEALTHY" nunca existiu;
    # sem "ONLINE" aqui o card ficava DEGRADED mesmo com ubus respondendo.
    state = {
        "ONLINE": "READY", "HEALTHY": "READY", "READY": "READY",
        "UNREACHABLE": "OFFLINE", "OFFLINE": "OFFLINE",
    }.get(overall, "DEGRADED")
    health = state
    if reachable and integration_error in {"REMOTE_AUTH_FAILED", "CAPABILITY_UNAVAILABLE"}:
        state = "DEGRADED"
        health = f"PING_OK_SSH_FALHOU ({integration_error})"
    return {
        "id": "openwrt", "name": "OpenWrt",
        "enabled": enabled and bool(getattr(host, "reachable", False)) or enabled,
        "configured": True,
        "connected": reachable,
        "state": state,
        "health": health,
        "latency_ms": icmp_latency,
        "last_sync": getattr(host, "observed_at", None),
        "last_error": integration_error,
        "address": getattr(host, "address", ""),
    }


# ---------------------------------------------------------------------------
# Ações reais por integração
# ---------------------------------------------------------------------------

class IntegrationActionError(Exception):
    def __init__(self, error_code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


async def integration_action(services: Any, integration_id: str, action: str) -> dict[str, Any]:
    if integration_id not in INTEGRATION_IDS:
        raise KeyError(integration_id)

    if action == "test":
        return await _action_test(services, integration_id)
    if action == "enable":
        return await _action_enable(services, integration_id, True)
    if action == "disable":
        return await _action_enable(services, integration_id, False)
    if action == "reconnect":
        return await _action_reconnect(services, integration_id)
    if action == "diagnostics":
        return await _action_diagnostics(services, integration_id)
    raise IntegrationActionError("ACTION_UNKNOWN", f"Ação '{action}' não suportada.")


async def _action_test(services: Any, integration_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    if integration_id == "sentinel":
        result = await services.sentinel.test_connection()
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result
    if integration_id == "home_assistant":
        from app.integrations.home_assistant_profiles import test_active_profile
        return await test_active_profile(services)
    if integration_id == "proxmox":
        from app.integrations.proxmox.config import test_connection as proxmox_test

        return await proxmox_test(services)
    if integration_id == "openwrt":
        card = await _openwrt_card(services)
        return {"ok": card["connected"], "latency_ms": card["latency_ms"],
                "state": card["state"], "detail": card["health"]}
    raise KeyError(integration_id)


async def _action_enable(services: Any, integration_id: str, enable: bool) -> dict[str, Any]:
    from app.core.runtime_settings import save_runtime_settings

    settings = services.settings
    if integration_id == "sentinel":
        connector = services.sentinel
        config = connector.config().model_copy(update={"enabled": enable})
        return await connector.update(config)
    if integration_id == "home_assistant":
        setattr(settings, "home_assistant_enabled", enable)
        save_runtime_settings({"home_assistant_enabled": enable})
        return {"id": integration_id, "enabled": enable}
    if integration_id == "proxmox":
        from app.integrations.proxmox.config import set_enabled as proxmox_set_enabled

        # prompt11_2 §3: persiste na FONTE AUTORITATIVA (proxmox-config.json)
        # além do runtime overlay — o estado sobrevive ao restart e status/test
        # nunca divergem (nada de enabled=true + PROXMOX_DISABLED).
        proxmox_set_enabled(enable)
        setattr(settings, "proxmox_enabled", enable)
        save_runtime_settings({"proxmox_enabled": enable})
        return {"id": integration_id, "enabled": enable,
                "restart_required": False}
    if integration_id == "openwrt":
        raise IntegrationActionError(
            "OPENWRT_MANAGED_BY_HOMELAB",
            "OpenWrt é gerenciado pelo Homelab Control Plane; habilite/desabilite o control plane.",
        )
    raise KeyError(integration_id)


async def _action_reconnect(services: Any, integration_id: str) -> dict[str, Any]:
    if integration_id == "sentinel":
        return await services.sentinel.reconnect()
    raise IntegrationActionError(
        "RECONNECT_NOT_APPLICABLE",
        "Reconexão explícita aplica-se apenas ao bridge Sentinel.",
    )


async def _action_diagnostics(services: Any, integration_id: str) -> dict[str, Any]:
    if integration_id == "sentinel":
        status = services.sentinel.status()
        summary = await services.sentinel.summary(hours=24)
        summary.pop("connection", None)
        return {"id": integration_id, "status": status, "summary_24h": summary}
    if integration_id == "home_assistant":
        from app.integrations.home_assistant_profiles import (
            list_profiles,
            unified_ha_state,
        )

        return {"id": integration_id,
                "status": await services.homelab.ha_status(),
                "unified": await unified_ha_state(services),
                "profiles": list_profiles()}
    if integration_id == "proxmox":
        from app.integrations.proxmox.config import public_status as proxmox_public_status
        from app.core.runtime_settings import load_runtime_settings

        settings = services.settings
        client = services.proxmox
        nodes: list[dict[str, Any]] | str | None = None
        if client.configured:
            try:
                raw_nodes = await asyncio.wait_for(client.nodes(), timeout=6)
                nodes = [
                    {"node": n.get("node"), "status": n.get("status")}
                    for n in (raw_nodes or [])[:10]
                    if isinstance(n, dict)
                ]
            except Exception as error:  # noqa: BLE001
                nodes = [f"erro: {type(error).__name__}"]
        runtime = load_runtime_settings()
        return {"id": integration_id,
                "status": proxmox_public_status(services),
                "runtime_settings_keys": sorted(
                    key for key in runtime if key.startswith("proxmox")
                ),
                "nodes": nodes}
    if integration_id == "openwrt":
        card = await _openwrt_card(services)
        return {"id": integration_id, **card}
    raise KeyError(integration_id)


async def _ha_profiles_summary() -> dict[str, Any]:
    from app.integrations.home_assistant_profiles import list_profiles

    data = list_profiles()
    return {"active_profile": data.get("active_profile"),
            "profiles": [
                {k: p.get(k) for k in
                 ("profile_id", "name", "enabled", "status", "last_test")}
                for p in data.get("profiles", [])
            ]}
