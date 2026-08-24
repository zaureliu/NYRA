"""Proxmox configuration via UI (prompt11_1 Parte C, §29-§34; prompt11_2 §3-§5).

Configuração completa da integração Proxmox pela interface:

    * campos NÃO secretos persistem em ``data/proxmox-config.json`` e são
      espelhados nas runtime settings (``proxmox_url``, ``proxmox_verify_ssl``);
    * API Token ID/Secret vivem EXCLUSIVAMENTE no Credential Broker
      (credential ids ``proxmox_api_token_id`` / ``proxmox_api_token_secret``),
      com fallback legado para settings (.env/config yaml) migrado em silêncio;
    * o frontend recebe somente metadados (``token_secret_configured: true``);
    * estados normalizados: DISABLED, UNCONFIGURED, AUTH_FAILED, READY,
      DEGRADED, OFFLINE, TLS_ERROR (§34) — READY exige token configurado.

prompt11_2 (hotfix de consistência): ``enabled`` tem FONTE ÚNICA resolvida em
``load_config`` com precedência arquivo → runtime overlay → settings legadas.
``public_status`` e ``test_connection`` usam EXATAMENTE essa resolução — é
impossível a UI mostrar ``Habilitada: Sim`` enquanto o teste responde
``PROXMOX_DISABLED`` (§3/§4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.paths import DATA_ROOT
from app.core.runtime_settings import load_runtime_settings
from app.integrations.base import IntegrationError

logger = logging.getLogger("nyra.proxmox_config")

CONFIG_PATH = DATA_ROOT / "proxmox-config.json"
_TOKEN_ID_CREDENTIAL = "proxmox_api_token_id"
_TOKEN_SECRET_CREDENTIAL = "proxmox_api_token_secret"
_STALE_AFTER_SECONDS = float(os.environ.get("NYRA_PROXMOX_STATE_STALE_SECONDS", "900"))

PROXMOX_STATES = (
    "DISABLED", "UNCONFIGURED", "AUTH_FAILED", "READY",
    "DEGRADED", "OFFLINE", "TLS_ERROR",
)


def _broker():
    from app.operator.credentials import CredentialBroker

    return CredentialBroker(approvals=None)


# ---------------------------------------------------------------- persistence

def _load_file(path: Path | None = None) -> dict[str, Any]:
    # Lê o global em tempo de chamada: permite isolamento por monkeypatch.
    path = path or CONFIG_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_file(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def load_config(settings: Any = None) -> dict[str, Any]:
    """Campos NÃO secretos; FONTE ÚNICA de verdade (prompt11_2 §3).

    Precedência por campo: ``proxmox-config.json`` → runtime overlay
    (``data/settings-v33.json``) → settings legadas (.env/pydantic).
    """
    stored = _load_file()
    overlay = load_runtime_settings()
    url = str(stored.get("url") or "")
    verify_ssl = bool(stored.get("verify_ssl", True))
    preferred_node = str(stored.get("preferred_node") or "")
    try:
        timeout = float(stored.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    if not timeout:
        timeout = 8.0
    if not url:
        url = str(overlay.get("proxmox_url") or "")
    if not url and settings is not None:
        url = str(getattr(settings, "proxmox_url", "") or "")
        verify_ssl = bool(getattr(settings, "proxmox_verify_ssl", True))
    if "enabled" in stored:
        enabled = bool(stored.get("enabled"))
    elif "proxmox_enabled" in overlay:
        enabled = bool(overlay.get("proxmox_enabled"))
    elif settings is not None:
        enabled = bool(getattr(settings, "proxmox_enabled", True))
    else:
        enabled = False
    return {
        "enabled": enabled,
        "url": url,
        "verify_ssl": verify_ssl,
        "preferred_node": preferred_node,
        "timeout_seconds": max(4.0, min(timeout, 60.0)),
        "last_test": stored.get("last_test") or {},
        "updated_at": stored.get("updated_at"),
    }


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Valida e persiste campos não secretos (arquivo atômico, merge por chave).

    prompt11_2: apenas chaves PRESENTES no payload são alteradas — um toggle
    ``{"enabled": false}`` não pode apagar a URL salva (§11 save flow).
    """
    updates: dict[str, Any] = {}
    if "url" in payload:
        url = str(payload.get("url") or "").strip().rstrip("/")
        if url and not url.lower().startswith(("http://", "https://")):
            raise ValueError("url deve começar com http:// ou https://")
        updates["url"] = url
    if "timeout_seconds" in payload:
        try:
            timeout = float(payload.get("timeout_seconds") or 8.0)
        except (TypeError, ValueError):
            raise ValueError("timeout_seconds inválido") from None
        updates["timeout_seconds"] = max(4.0, min(timeout, 60.0))
    if "verify_ssl" in payload:
        updates["verify_ssl"] = bool(payload.get("verify_ssl"))
    if "preferred_node" in payload:
        updates["preferred_node"] = str(payload.get("preferred_node") or "")[:64]
    if "enabled" in payload:
        updates["enabled"] = bool(payload.get("enabled"))
    stored = _load_file()
    stored.update(updates)
    stored["updated_at"] = time.time()
    _save_file(stored)
    return load_config()


def set_enabled(enabled: bool) -> dict[str, Any]:
    """Alterna enabled na fonte autoritativa preservando os demais campos."""
    return save_config({"enabled": bool(enabled)})


def record_test_result(result: dict[str, Any]) -> None:
    stored = _load_file()
    stored["last_test"] = {**result, "tested_at": time.time()}
    _save_file(stored)


# ---------------------------------------------------------------- credentials

def resolve_credentials(settings: Any) -> tuple[str, str]:
    """PONTO ÚNICO de resolução das credenciais Proxmox (Broker → legado).

    Valores legados encontrados nas settings (.env/config yaml) são migrados
    silenciosamente para o Broker sem nunca serem impressos (§7/§31).
    """
    token_id = ""
    token_secret = ""
    try:
        broker = _broker()
        token_id = str(broker.resolve(_TOKEN_ID_CREDENTIAL) or "").strip()
        token_secret = str(broker.resolve(_TOKEN_SECRET_CREDENTIAL) or "").strip()
    except Exception as error:  # noqa: BLE001 - broker indisponível usa legado
        logger.warning("proxmox_broker_resolve_failed type=%s", type(error).__name__)
        broker = None
    legacy_id = str(getattr(settings, "proxmox_token_id", "") or "").strip()
    legacy_secret = str(getattr(settings, "proxmox_token_secret", "") or "").strip()
    migrated = False
    if legacy_id and not token_id:
        token_id = legacy_id
        migrated = True
    if legacy_secret and not token_secret:
        token_secret = legacy_secret
        migrated = True
    if migrated and broker is not None:
        try:
            if legacy_id and legacy_id == token_id:
                broker.create(_TOKEN_ID_CREDENTIAL, legacy_id, kind="proxmox_token_id",
                              description="Proxmox API Token ID (migrado)", operator_direct=True)
            if legacy_secret and legacy_secret == token_secret:
                broker.create(_TOKEN_SECRET_CREDENTIAL, legacy_secret, kind="proxmox_token_secret",
                              description="Proxmox API Token Secret (migrado)", operator_direct=True)
        except Exception as error:  # noqa: BLE001 - segue funcional com o valor legado
            logger.info("proxmox_broker_migration_failed type=%s", type(error).__name__)
    return token_id, token_secret


def save_credentials(token_id: str, token_secret: str) -> dict[str, bool]:
    """Persiste novas credenciais no Broker (uso direto do operador pela UI)."""
    cleaned_id = token_id.strip()
    cleaned_secret = token_secret.strip()
    if not cleaned_id or not cleaned_secret:
        raise ValueError("Token ID e Secret são obrigatórios.")
    broker = _broker()
    broker.create(_TOKEN_ID_CREDENTIAL, cleaned_id, kind="proxmox_token_id",
                  description="Proxmox API Token ID", operator_direct=True)
    broker.create(_TOKEN_SECRET_CREDENTIAL, cleaned_secret, kind="proxmox_token_secret",
                  description="Proxmox API Token Secret", operator_direct=True)
    return {"token_id_configured": True, "token_secret_configured": True}


def disconnect_credentials() -> dict[str, bool]:
    """'Disconnect': esquece credenciais do Broker (não toca no legado .env)."""
    removed = {"token_id_removed": False, "token_secret_removed": False}
    try:
        broker = _broker()
        removed["token_id_removed"] = bool(broker.delete(_TOKEN_ID_CREDENTIAL))
        removed["token_secret_removed"] = bool(broker.delete(_TOKEN_SECRET_CREDENTIAL))
    except Exception as error:  # noqa: BLE001
        logger.warning("proxmox_disconnect_failed type=%s", type(error).__name__)
    return removed


def apply_to_runtime(services: Any) -> dict[str, Any]:
    """Aplica config salva aos clients reais em runtime (sem restart)."""
    summary: dict[str, Any] = {"applied": False}
    try:
        settings = services.settings
        config = load_config(settings)
        token_id, token_secret = resolve_credentials(settings)
        url = config["url"] or str(getattr(settings, "proxmox_url", "") or "")
        for attr, value in (("proxmox_url", url),
                            ("proxmox_verify_ssl", config["verify_ssl"]),
                            ("proxmox_enabled", config["enabled"])):
            if hasattr(settings, attr):
                setattr(settings, attr, value)
        applied_clients = 0
        seen: set[int] = set()
        for client in (getattr(services, "proxmox", None),
                       getattr(getattr(services, "homelab", None), "proxmox", None)):
            if client is None or id(client) in seen or not hasattr(client, "set_credentials"):
                continue
            seen.add(id(client))
            client.set_credentials(url, token_id, token_secret,
                                   verify_ssl=config["verify_ssl"])
            applied_clients += 1
        summary.update({
            "applied": applied_clients > 0,
            "clients": applied_clients,
            "auth_configured": bool(token_id and token_secret),
            "url": url,
        })
    except Exception as error:  # noqa: BLE001
        logger.warning("proxmox_runtime_apply_failed type=%s", type(error).__name__)
    return summary


# -------------------------------------------------------------------- status

def public_status(services: Any) -> dict[str, Any]:
    """Snapshot público (sem secrets) para cards/formulários (§40).

    prompt11_2 §4: ``enabled`` vem SEMPRE de ``load_config`` (fonte única);
    sem token com enabled=true o estado é UNCONFIGURED — nunca DISABLED.
    """
    settings = services.settings
    config = load_config(settings)
    client = getattr(services, "proxmox", None)
    token_id, token_secret = resolve_credentials(settings)
    auth_configured = bool(token_id and token_secret)
    enabled = bool(config["enabled"])

    last_test = config.get("last_test") or {}
    tested_at = last_test.get("tested_at")
    age = (time.time() - float(tested_at)) if tested_at else None

    if not enabled:
        state, detail = "DISABLED", "Integração desabilitada"
    elif not config["url"]:
        state, detail = "UNCONFIGURED", "URL não configurada"
    elif not auth_configured:
        # §34/§69: Proxmox NUNCA é READY sem API token.
        state, detail = "UNCONFIGURED", "API Token ausente"
    else:
        if last_test.get("ok"):
            state = "STALE" if age is not None and age > _STALE_AFTER_SECONDS else "READY"
            detail = "Teste autenticado bem-sucedido" if state == "READY" else f"Último sucesso há {int(age or 0)}s"
        elif last_test.get("state") == "AUTH_FAILED":
            state, detail = "AUTH_FAILED", "Credencial recusada pelo Proxmox"
        elif last_test.get("state") == "TLS_ERROR":
            state, detail = "TLS_ERROR", "Falha de validação TLS"
        elif last_test.get("state") == "OFFLINE":
            state, detail = "OFFLINE", "Host inacessível no último teste"
        elif last_test:
            state, detail = "DEGRADED", f"Falha: {last_test.get('error_code') or 'desconhecida'}"
        else:
            state, detail = "DEGRADED", "Configurado; aguardando primeiro teste"

    configured = bool(config["url"]) and auth_configured
    return {
        "id": "proxmox",
        "enabled": enabled,
        "configured": configured,
        "url": config["url"],
        "url_configured": bool(config["url"]),
        "verify_ssl": config["verify_ssl"],
        "preferred_node": config["preferred_node"],
        "timeout_seconds": config["timeout_seconds"],
        "token_id_configured": bool(token_id),
        "token_secret_configured": bool(token_secret),
        "auth_configured": auth_configured,
        "authenticated": bool(state in {"READY", "STALE"} and last_test.get("ok")),
        "state": state,
        "health": detail,
        "latency_ms": last_test.get("latency_ms"),
        "version": last_test.get("version"),
        "node_count": last_test.get("node_count"),
        "qemu_count": last_test.get("qemu_count"),
        "lxc_count": last_test.get("lxc_count"),
        "storage_count": last_test.get("storage_count"),
        "last_test": last_test.get("tested_at"),
        "last_success": last_test.get("tested_at") if last_test.get("ok") else None,
        "last_error": None if state in {"READY", "DEGRADED"} else detail,
        "open_url": config["url"] or None,
    }


# ------------------------------------------------------------ test connection

async def test_connection(services: Any) -> dict[str, Any]:
    """Test Connection real (§33): version/nodes/vms/storage + latência.

    Estados: UNCONFIGURED (sem token), AUTH_FAILED (401), TLS_ERROR,
    OFFLINE (sem conectividade), PROXMOX_API_ERROR para o resto.
    """
    apply_to_runtime(services)
    settings = services.settings
    client = getattr(services, "proxmox", None)
    # prompt11_2 §4: mesma resolução de enabled do public_status (fonte única).
    # É IMPOSSÍVEL status=Habilitada + test=PROXMOX_DISABLED divergirem.
    if not load_config(settings)["enabled"]:
        result = {"ok": False, "state": "DISABLED", "error_code": "PROXMOX_DISABLED"}
        record_test_result({"ok": False, "state": "DISABLED", "error_code": "PROXMOX_DISABLED"})
        return result
    token_id, token_secret = resolve_credentials(settings)
    config = load_config(settings)
    if not config["url"] or not token_id or not token_secret:
        if client is not None and not client.configured:
            result = {"ok": False, "state": "UNCONFIGURED",
                      "error_code": "PROXMOX_UNCONFIGURED",
                      "message": "URL ou API Token ausente."}
            record_test_result({"ok": False, "state": "UNCONFIGURED",
                                "error_code": "PROXMOX_UNCONFIGURED"})
            return result
    started = time.perf_counter()
    result: dict[str, Any]
    try:
        loop_timeout = float(config["timeout_seconds"])
        version = await asyncio.wait_for(client.version(), timeout=loop_timeout)
        nodes = await asyncio.wait_for(client.nodes(), timeout=loop_timeout)
        guests = await asyncio.wait_for(client.virtual_machines(), timeout=loop_timeout)
        storage = await asyncio.wait_for(client.storage(), timeout=loop_timeout)
        qemu = sum(1 for g in guests if str(g.get("type")) == "qemu")
        lxc = sum(1 for g in guests if str(g.get("type")) == "lxc")
        running = sum(1 for g in guests if str(g.get("status")) == "running")
        result = {
            "ok": True,
            "state": "READY",
            "authenticated": True,
            "version": str((version or {}).get("version", ""))[:24],
            "node_count": len(nodes or []),
            "nodes_online": sum(1 for n in nodes or [] if str(n.get("status")) == "online"),
            "qemu_count": qemu,
            "lxc_count": lxc,
            "guest_count": len(guests or []),
            "running_guests": running,
            "storage_count": len(storage or []),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except IntegrationError as error:
        code = str(error.code or "")
        if code.endswith("_AUTH_FAILED") or code.endswith("_AUTH_MISSING"):
            result = {"ok": False, "state": "AUTH_FAILED",
                      "error_code": "PROXMOX_AUTH_FAILED", "message": error.message}
        elif "TLS" in code:
            result = {"ok": False, "state": "TLS_ERROR", "error_code": code,
                      "message": error.message}
        else:
            result = {"ok": False, "state": "DEGRADED", "error_code": code,
                      "message": error.message}
    except httpx.TimeoutException:
        result = {"ok": False, "state": "OFFLINE", "error_code": "PROXMOX_TIMEOUT"}
    except (httpx.HTTPError, OSError, TimeoutError) as error:
        result = {"ok": False, "state": "OFFLINE", "error_code": "PROXMOX_OFFLINE",
                  "error_type": type(error).__name__}
    record_test_result({key: result.get(key) for key in
                        ("ok", "state", "error_code", "version", "node_count",
                         "qemu_count", "lxc_count", "storage_count", "latency_ms")})
    return result


# ------------------------------------------------------------------ inventory

async def inventory(services: Any) -> dict[str, Any]:
    """Inventário normalizado (§35): nodes / guests QEMU+LXC / storage."""
    settings = services.settings
    controller = getattr(services, "homelab", None)
    if controller is None:
        raise IntegrationError("PROXMOX_API_ERROR", "Homelab Control Plane indisponível.")
    nodes_raw = await controller.proxmox.nodes() if hasattr(controller.proxmox, "nodes") else []
    nodes = [
        {
            "node": str(n.get("node") or "")[:64],
            "state": str(n.get("status") or "")[:24],
            "cpu_percent": round(float(n.get("cpu") or 0) * 100, 1),
            "memory_used_bytes": n.get("mem"),
            "memory_total_bytes": n.get("maxmem"),
            "uptime_s": n.get("uptime"),
        }
        for n in (nodes_raw or []) if isinstance(n, dict)
    ]
    guests = await controller.proxmox_list_vms(include_lxc=True)
    for guest in guests:
        guest["type"] = guest.pop("guest_type", "qemu")
    storages = await controller.proxmox_storage_status()
    return {
        "nodes": nodes,
        "qemu": [g for g in guests if g.get("type") == "qemu"],
        "lxc": [g for g in guests if g.get("type") == "lxc"],
        "storage": storages,
        "generated_at": time.time(),
    }
