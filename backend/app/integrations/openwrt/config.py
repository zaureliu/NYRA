"""OpenWrt configuration via UI (hotfix openwrt_config_hotfix.md).

Configuração da integração OpenWrt pela interface, seguindo a MESMA
arquitetura do card Proxmox (nenhum adapter/broker paralelo):

    * campos NÃO secretos (host/url, usuário SSH) persistem em
      ``data/openwrt-config.json`` com fallback para runtime overlay e
      settings legadas (``openwrt_url`` / ``openwrt_username``);
    * a senha SSH vive EXCLUSIVAMENTE no Credential Broker (credential id
      ``openwrt_ssh_password``), com migração silenciosa do legado
      ``settings.openwrt_password``; o frontend recebe somente metadados;
    * Testar conexão usa o ADAPTER EXISTENTE: Homelab Control Plane →
      ``OpenWrtAdapter`` → ``RemoteShellService`` (Trusted Host Registry,
      risco READ_ONLY, redaction, auditoria);
    * estados coerentes: DISABLED, UNCONFIGURED (sem credencial /
      AUTH_MISSING), AUTH_FAILED (credencial recusada), OFFLINE (host
      inalcançável), READY (auth válida) e DEGRADED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT
from app.core.runtime_settings import load_runtime_settings
from app.homelab.adapters.base import SshAdapterError
from app.integrations.base import IntegrationError

logger = logging.getLogger("kazumi.openwrt_config")

CONFIG_PATH = DATA_ROOT / "openwrt-config.json"
_PASSWORD_CREDENTIAL = "openwrt_ssh_password"
_STALE_AFTER_SECONDS = float(os.environ.get("KAZUMI_OPENWRT_STATE_STALE_SECONDS", "900"))
_TEST_TIMEOUT_SECONDS = float(os.environ.get("KAZUMI_OPENWRT_TEST_TIMEOUT_SECONDS", "30"))

OPENWRT_STATES = (
    "DISABLED", "UNCONFIGURED", "AUTH_FAILED", "READY",
    "DEGRADED", "OFFLINE",
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
    """Campos NÃO secretos; fonte única com precedência
    ``openwrt-config.json`` → runtime overlay → settings legadas."""
    stored = _load_file()
    overlay = load_runtime_settings()
    url = str(stored.get("url") or "")
    username = str(stored.get("username") or "")
    if not url:
        url = str(overlay.get("openwrt_url") or "")
    if not username:
        username = str(overlay.get("openwrt_username") or "")
    if not url and settings is not None:
        url = str(getattr(settings, "openwrt_url", "") or "")
    if not username and settings is not None:
        username = str(getattr(settings, "openwrt_username", "") or "")
    return {
        "url": url.strip().rstrip("/"),
        "username": username.strip()[:120],
        "last_test": stored.get("last_test") or {},
        "updated_at": stored.get("updated_at"),
    }


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Valida e persiste campos não secretos (merge por chave — chaves
    ausentes no payload NUNCA sobrescrevem o que já está salvo)."""
    updates: dict[str, Any] = {}
    if "url" in payload:
        url = str(payload.get("url") or "").strip().rstrip("/")
        if len(url) > 200:
            raise ValueError("url excede 200 caracteres")
        updates["url"] = url[:200]
    if "username" in payload:
        updates["username"] = str(payload.get("username") or "").strip()[:120]
    stored = _load_file()
    stored.update(updates)
    stored["updated_at"] = time.time()
    _save_file(stored)
    return load_config()


def record_test_result(result: dict[str, Any]) -> None:
    stored = _load_file()
    stored["last_test"] = {**result, "tested_at": time.time()}
    _save_file(stored)


# ---------------------------------------------------------------- credentials

def resolve_password(settings: Any) -> str:
    """PONTO ÚNICO de resolução da senha SSH (Broker → legado silencioso).

    Valores legados em settings (.env/config yaml) migram para o Broker sem
    nunca serem logados ou devolvidos ao frontend.
    """
    password = ""
    broker = None
    try:
        broker = _broker()
        password = str(broker.resolve(_PASSWORD_CREDENTIAL) or "").strip()
    except Exception as error:  # noqa: BLE001 - broker indisponível usa legado
        logger.warning("openwrt_broker_resolve_failed type=%s", type(error).__name__)
    legacy = str(getattr(settings, "openwrt_password", "") or "").strip()
    if legacy and not password:
        password = legacy
        if broker is not None:
            try:
                broker.create(_PASSWORD_CREDENTIAL, legacy, kind="openwrt_ssh_password",
                              description="Senha SSH OpenWrt (migrada)", operator_direct=True)
            except Exception as error:  # noqa: BLE001 - segue funcional com o legado
                logger.info("openwrt_broker_migration_failed type=%s", type(error).__name__)
    return password


def save_credentials(password: str) -> dict[str, bool]:
    """Persiste a senha SSH no Credential Broker (uso direto do operador)."""
    cleaned = password.strip()
    if not cleaned:
        raise ValueError("Senha SSH é obrigatória.")
    broker = _broker()
    broker.create(_PASSWORD_CREDENTIAL, cleaned, kind="openwrt_ssh_password",
                  description="Senha SSH OpenWrt", operator_direct=True)
    return {"password_configured": True}


# -------------------------------------------------------------------- runtime

def apply_to_runtime(services: Any) -> dict[str, Any]:
    """Espelha a config salva nos atributos runtime (somente memória)."""
    summary: dict[str, Any] = {"applied": False}
    try:
        settings = services.settings
        config = load_config(settings)
        password = resolve_password(settings)
        for attr, value in (("openwrt_url", config["url"]),
                            ("openwrt_username", config["username"]),
                            ("openwrt_password", password)):
            if hasattr(settings, attr):
                setattr(settings, attr, value)
        summary.update({
            "applied": True,
            "auth_configured": bool(password),
            "url": config["url"],
        })
    except Exception as error:  # noqa: BLE001
        logger.warning("openwrt_runtime_apply_failed type=%s", type(error).__name__)
    return summary


# --------------------------------------------------------------------- status

def public_status(services: Any) -> dict[str, Any]:
    """Snapshot público (sem secrets) para cards/formulários.

    Sem credencial o estado é UNCONFIGURED — nunca READY (§7 do hotfix).
    """
    settings = services.settings
    config = load_config(settings)
    auth_configured = bool(resolve_password(settings))
    enabled = bool(getattr(settings, "homelab_enabled", True))

    last_test = config.get("last_test") or {}
    tested_at = last_test.get("tested_at")
    age = (time.time() - float(tested_at)) if tested_at else None

    if not enabled:
        state, detail = "DISABLED", "Homelab Control Plane desabilitado"
    elif not config["url"]:
        state, detail = "UNCONFIGURED", "Host/URL não configurado"
    elif not auth_configured:
        # AUTH_MISSING: host configurado mas senha SSH ausente.
        state, detail = "UNCONFIGURED", "AUTH_MISSING — senha SSH ausente"
    else:
        if last_test.get("ok"):
            state = "STALE" if age is not None and age > _STALE_AFTER_SECONDS else "READY"
            detail = "Teste SSH autenticado bem-sucedido" if state == "READY" \
                else f"Último sucesso há {int(age or 0)}s"
        elif last_test.get("state") == "AUTH_FAILED":
            state, detail = "AUTH_FAILED", "Credencial recusada pelo host (SSH)"
        elif last_test.get("state") == "OFFLINE":
            state, detail = "OFFLINE", "Host inalcançável no último teste"
        elif last_test:
            state, detail = "DEGRADED", f"Falha: {last_test.get('error_code') or 'desconhecida'}"
        else:
            state, detail = "DEGRADED", "Configurado; aguardando primeiro teste"

    configured = bool(config["url"]) and auth_configured
    return {
        "id": "openwrt",
        "enabled": enabled,
        "configured": configured,
        "url": config["url"],
        "url_configured": bool(config["url"]),
        "username": config["username"],
        "username_configured": bool(config["username"]),
        "password_configured": auth_configured,
        "auth_configured": auth_configured,
        "authenticated": bool(state == "READY" and last_test.get("ok")),
        "state": state,
        "health": detail,
        "latency_ms": last_test.get("latency_ms"),
        "version": last_test.get("version"),
        "uptime_s": last_test.get("uptime_s"),
        "last_test": last_test.get("tested_at"),
        "last_success": last_test.get("tested_at") if last_test.get("ok") else None,
        "last_error": None if state in {"READY", "DEGRADED"} else detail,
    }


# ------------------------------------------------------------ test connection

async def test_connection(services: Any) -> dict[str, Any]:
    """Test Connection REAL via adapter existente (hotfix §6).

    Caminho: ``services.homelab.openwrt_status()`` → ``OpenWrtAdapter`` →
    ``RemoteShellService``. Estados resultantes:

        * sem credencial            → UNCONFIGURED / REMOTE_AUTH_MISSING
        * credencial recusada       → AUTH_FAILED   / REMOTE_AUTH_FAILED
        * host inalcançável         → OFFLINE
        * status ubus obtido        → READY
    """
    apply_to_runtime(services)
    settings = services.settings
    config = load_config(settings)
    password = resolve_password(settings)
    if not config["url"] or not password:
        result = {"ok": False, "state": "UNCONFIGURED",
                  "error_code": "REMOTE_AUTH_MISSING",
                  "message": "Host/URL ou senha SSH ausente."}
        record_test_result({"ok": False, "state": "UNCONFIGURED",
                            "error_code": "REMOTE_AUTH_MISSING"})
        return result
    controller = getattr(services, "homelab", None)
    if controller is None or not hasattr(controller, "openwrt_status"):
        result = {"ok": False, "state": "DEGRADED",
                  "error_code": "HOMELAB_CONTROL_PLANE_UNAVAILABLE",
                  "message": "Homelab Control Plane indisponível."}
        record_test_result({"ok": False, "state": "DEGRADED",
                            "error_code": "HOMELAB_CONTROL_PLANE_UNAVAILABLE"})
        return result

    started = time.perf_counter()
    result: dict[str, Any]
    try:
        status = await asyncio.wait_for(
            controller.openwrt_status(), timeout=_TEST_TIMEOUT_SECONDS)
        release = status.get("release") or {} if isinstance(status, dict) else {}
        version = str(release.get("DISTRIB_DESCRIPTION")
                      or release.get("DISTRIB_RELEASE") or "")[:48]
        result = {
            "ok": True,
            "state": "READY",
            "authenticated": True,
            "version": version,
            "uptime_s": status.get("uptime_s") if isinstance(status, dict) else None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except SshAdapterError as error:
        code = str(error.code or "")
        if code == "REMOTE_AUTH_FAILED":
            result = {"ok": False, "state": "AUTH_FAILED",
                      "error_code": code, "message": error.message}
        elif code in {"REMOTE_AUTH_MISSING", "HOMELAB_HOST_UNKNOWN"}:
            result = {"ok": False, "state": "UNCONFIGURED",
                      "error_code": code, "message": error.message}
        elif code == "HOMELAB_HOST_DISABLED":
            result = {"ok": False, "state": "DISABLED",
                      "error_code": code, "message": error.message}
        elif code == "REMOTE_EXECUTION_FAILED":
            result = {"ok": False, "state": "OFFLINE",
                      "error_code": code, "message": error.message}
        else:
            result = {"ok": False, "state": "DEGRADED",
                      "error_code": code, "message": error.message}
    except IntegrationError as error:
        result = {"ok": False, "state": "DEGRADED",
                  "error_code": str(error.code or ""), "message": error.message}
    except asyncio.TimeoutError:
        result = {"ok": False, "state": "OFFLINE", "error_code": "OPENWRT_TIMEOUT",
                  "message": "Teste excedeu o timeout."}
    except OSError as error:
        result = {"ok": False, "state": "OFFLINE", "error_code": "OPENWRT_OFFLINE",
                  "error_type": type(error).__name__}
    record_test_result({key: result.get(key) for key in
                        ("ok", "state", "error_code", "version", "uptime_s",
                         "latency_ms")})
    return result
