"""Home Assistant Profiles V3 (prompt11 Parte N, §76-§84) + V11 (prompt11_1).

Profiles reais e persistentes para a integração Home Assistant:

    * ``ha-vm``        — perfil de VM sem URL pública predefinida.
    * ``ha-physical``  — placeholder físico-ready (nunca contatado até existir
      hardware configurado; §191).

Resolução de credencial ÚNICA (prompt11_1 §6/§7): o fluxo autoritativo é

    perfil ativo → credential_id → Credential Broker → Bearer

com migração silenciosa de configuração legada (.env ``KAZUMI_HOME_ASSISTANT_TOKEN``
e arquivo por-perfil em ``data/secrets``): o valor funcional é reutilizado,
nunca apagado, nunca impresso e nunca gerado automaticamente.

Regressão corrigida (§4-§16): probes NUNCA contatam endpoints autenticados sem
Bearer; sem token o estado é UNCONFIGURED; 401/403 viram AUTH_FAILED; o User-Agent
padrão ``python-httpx`` foi substituído pelo UA identificado da KAZUMI em todas as
chamadas. ``READY`` exige reachable + authenticated validados por teste real.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.paths import DATA_ROOT
from app.integrations.base import IntegrationError, require_secure_credential_transport

logger = logging.getLogger("kazumi.ha_profiles")

PROFILES_PATH = DATA_ROOT / "ha-profiles.json"
SECRETS_DIR = DATA_ROOT / "secrets"

USER_AGENT = "KAZUMI-Homelab/1.0"
_BROKER_CREDENTIAL_PREFIX = "homeassistant_token_"
# Janela após a qual um último sucesso deixa de ser considerado fresco (STALE).
_STALE_AFTER_SECONDS = float(os.environ.get("KAZUMI_HA_STATE_STALE_SECONDS", "900"))

DEFAULT_PROFILES: list[dict[str, Any]] = [
    {
        "profile_id": "ha-vm",
        "name": "Home Assistant VM",
        "enabled": True,
        "url": "",
        "tls": False,
        "priority": 1,
    },
    {
        "profile_id": "ha-physical",
        "name": "Home Assistant Physical",
        "enabled": False,
        "url": "",
        "tls": True,
        "priority": 2,
    },
]

HA_STATES = (
    "DISABLED", "UNCONFIGURED", "STARTING", "READY",
    "AUTH_FAILED", "DEGRADED", "OFFLINE", "STALE",
)


def _token_path(profile_id: str) -> Path:
    return SECRETS_DIR / f"home-assistant-token-{profile_id}.txt"


def _credential_id(profile_id: str) -> str:
    return f"{_BROKER_CREDENTIAL_PREFIX}{profile_id}"


def _broker():
    """Credential Broker compartilhado (sem gate: uso interno do backend).

    A criação usa ``operator_direct=True`` apenas para MIGRAÇÃO de credencial
    legada já fornecida pelo operador (.env/arquivo) — nenhum caminho LLM passa
    por aqui (spec §89-§90).
    """
    from app.operator.credentials import CredentialBroker

    return CredentialBroker(approvals=None)


class HAProfileSecretStore:
    """Resolução única do token por perfil: env → Broker → arquivo legado.

    O valor legado encontrado é migrado silenciosamente para o Credential
    Broker (§7): reutilizado, não reimpresso e jamais removido sem motivo.
    """

    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id

    # ------------------------------------------------------------------ write
    def save(self, token: str) -> None:
        cleaned = token.strip()
        if len(cleaned) < 16:
            raise ValueError("Token Home Assistant muito curto")
        stored = False
        try:
            _broker().create(
                _credential_id(self.profile_id), cleaned,
                kind="http_bearer",
                description=f"Home Assistant long-lived token ({self.profile_id})",
                operator_direct=True,
            )
            stored = True
        except Exception as error:  # noqa: BLE001 - broker indisponível cai no arquivo local
            logger.warning("ha_token_broker_store_failed type=%s", type(error).__name__)
        if not stored:
            self._write_legacy_file(cleaned)
        self._tombstone_path().unlink(missing_ok=True)

    def clear(self) -> None:
        tombstone = self._tombstone_path()
        tombstone.parent.mkdir(parents=True, exist_ok=True)
        tombstone.write_text("credentials disabled by operator\n", encoding="utf-8")
        try:
            broker = _broker()
            try:
                broker.delete(_credential_id(self.profile_id), operator_direct=True)
            except TypeError:  # compatibility with narrow legacy/test adapters
                broker.delete(_credential_id(self.profile_id))
        except Exception:  # noqa: BLE001 - best-effort
            pass
        try:
            _token_path(self.profile_id).unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------- read
    def load(self) -> str:
        if self._tombstone_path().is_file():
            return ""
        env_value = (os.environ.get("KAZUMI_HOME_ASSISTANT_TOKEN") or "").strip()
        if env_value:
            return env_value
        try:
            resolved = _broker().resolve(_credential_id(self.profile_id))
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved:
            return str(resolved).strip()
        return self._load_legacy_file()

    def configured(self) -> bool:
        return bool(self.load())

    def _tombstone_path(self) -> Path:
        return SECRETS_DIR / f"home-assistant-{self.profile_id}.disabled"

    # ------------------------------------------------------------------ legacy
    def _legacy_file_value(self) -> str:
        try:
            return _token_path(self.profile_id).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _load_legacy_file(self) -> str:
        value = self._legacy_file_value()
        if not value:
            return ""
        try:
            current = _broker().resolve(_credential_id(self.profile_id))
        except Exception:  # noqa: BLE001
            current = None
        if current and str(current).strip() == value:
            return value
        # Migração silenciosa da credencial funcional para o Broker (§7).
        try:
            _broker().create(
                _credential_id(self.profile_id), value,
                kind="http_bearer",
                description=f"Home Assistant long-lived token ({self.profile_id})",
                operator_direct=True,
            )
        except Exception as error:  # noqa: BLE001 - segue funcional com o valor legado
            logger.info("ha_token_broker_migration_failed type=%s", type(error).__name__)
        return value

    def _write_legacy_file(self, cleaned: str) -> None:
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        path = _token_path(self.profile_id)
        path.write_text(cleaned + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Funções públicas de resolução
# ---------------------------------------------------------------------------


def resolve_profile_token(profile_id: str, settings: Any = None) -> str:
    """PONTO ÚNICO de resolução do token HA para clients e probes (§6).

    Ordem: perfil (env/Broker/arquivo legado) → settings legadas (.env via
    pydantic). O valor das settings NÃO é duplicado em disco: continua sendo
    apenas uma fonte de resolução da mesma função autoritativa (§7).
    """
    store = HAProfileSecretStore(profile_id)
    token = store.load()
    if token:
        return token
    if settings is not None and not store._tombstone_path().is_file():
        return str(getattr(settings, "home_assistant_token", "") or "").strip()
    return ""


def active_profile_id() -> str | None:
    data = _load_store()
    active = data.get("active_profile")
    known = {p.get("profile_id") for p in data["profiles"]}
    if active in known:
        return str(active)
    for profile in sorted(data["profiles"], key=lambda p: int(p.get("priority") or 99)):
        if profile.get("enabled"):
            return str(profile.get("profile_id"))
    return None


def _load_store(path: Path | None = None) -> dict[str, Any]:
    # Lê o global em tempo de chamada: permite isolamento por monkeypatch.
    path = path or PROFILES_PATH
    if not path.is_file():
        return {"version": 1, "active_profile": "",
                "profiles": [dict(p) for p in DEFAULT_PROFILES]}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            data.setdefault("version", 1)
            data.setdefault("active_profile", "")
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "active_profile": "",
            "profiles": [dict(p) for p in DEFAULT_PROFILES]}


def _save_store(data: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or PROFILES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return data


def derive_status(enabled: bool, url: str, auth_configured: bool,
                  stored_status: str, last_test: dict[str, Any] | None) -> str:
    """Deriva o estado público do perfil SEM permitir Auth Ausente + READY.

    Invariante prompt11_1 §13/§69: sem credencial configurada o estado só pode
    ser UNCONFIGURED (ou DISABLED), mesmo que um status antigo diga READY.
    """
    if not enabled:
        return "DISABLED"
    if not url or not auth_configured:
        return "UNCONFIGURED"
    clean_status = str(stored_status or "").upper()
    if clean_status == "READY":
        return "READY"
    if clean_status in {"AUTH_FAILED", "OFFLINE", "DEGRADED", "STALE"}:
        return clean_status
    if clean_status in {"DISABLED", "UNCONFIGURED"} or not last_test:
        return "STARTING"
    return "STARTING"


def _public(profile: dict[str, Any]) -> dict[str, Any]:
    store = HAProfileSecretStore(str(profile.get("profile_id")))
    enabled = bool(profile.get("enabled"))
    url = str(profile.get("url") or "")
    auth = store.configured()
    last_test = profile.get("last_test") if isinstance(profile.get("last_test"), dict) else None
    return {
        "profile_id": profile.get("profile_id"),
        "name": profile.get("name"),
        "enabled": enabled,
        "url": url,
        "tls": bool(profile.get("tls")),
        "priority": int(profile.get("priority") or 99),
        "auth_configured": auth,
        "status": derive_status(enabled, url, auth,
                                str(profile.get("status") or ""), last_test),
        "last_test": last_test,
    }


def list_profiles() -> dict[str, Any]:
    data = _load_store()
    profiles = sorted(
        (_public(p) for p in data["profiles"]),
        key=lambda p: p["priority"],
    )
    active = data.get("active_profile")
    known_ids = {p["profile_id"] for p in data["profiles"]}
    if active not in known_ids:
        active = next((p["profile_id"] for p in profiles if p["enabled"]), None)
    return {"version": 1, "active_profile": active, "profiles": profiles}


def get_profile(profile_id: str) -> dict[str, Any]:
    data = _load_store()
    for profile in data["profiles"]:
        if profile.get("profile_id") == profile_id:
            return _public(profile)
    raise KeyError(profile_id)


def _validated_profile_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise ValueError("porta da URL inválida") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("URL Home Assistant inválida; use somente a origem http(s)://host:porta")
    _ = parsed_port
    return url


def _endpoint_identity(url: str) -> tuple[str, str, int] | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port
    except ValueError:
        return "invalid", "", -1


def upsert_profile(payload: dict[str, Any], settings: Any = None) -> dict[str, Any]:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id or len(profile_id) > 64 or not all(
        c.isalnum() or c in "-_" for c in profile_id
    ):
        raise ValueError("profile_id inválido")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("nome inválido")
    url = _validated_profile_url(payload.get("url"))

    data = _load_store()
    existing = next((p for p in data["profiles"] if p.get("profile_id") == profile_id), None)
    had_credentials = bool(resolve_profile_token(profile_id, settings)) if existing is not None else False
    endpoint_changed = bool(
        existing is not None
        and _endpoint_identity(str(existing.get("url") or "")) != _endpoint_identity(url)
    )
    credentials_reset = endpoint_changed and had_credentials
    record = existing or {
        "profile_id": profile_id, "status": "", "last_test": None,
    }
    record.update({
        "profile_id": profile_id,
        "name": name,
        "enabled": bool(payload.get("enabled", existing.get("enabled", False) if existing else False)),
        "url": url,
        "tls": bool(payload.get("tls", False)),
        "priority": max(1, min(int(payload.get("priority") or 99), 999)),
    })
    if existing is None:
        data["profiles"].append(record)
    _save_store(data)
    if endpoint_changed:
        # A credencial pertence à origem anterior. Exigir um novo fornecimento
        # impede que uma simples edição de URL redirecione o Bearer existente.
        if credentials_reset:
            HAProfileSecretStore(profile_id).clear()
        record["status"] = "UNCONFIGURED"
        record["last_test"] = None
        _save_store(data)
    public = _public(record)
    public["endpoint_changed"] = endpoint_changed
    public["credentials_reset"] = credentials_reset
    return public


def remove_profile(profile_id: str) -> dict[str, Any]:
    if profile_id == "ha-vm":
        raise PermissionError("O profile padrão da VM não pode ser removido")
    data = _load_store()
    before = len(data["profiles"])
    data["profiles"] = [p for p in data["profiles"] if p.get("profile_id") != profile_id]
    if len(data["profiles"]) == before:
        raise KeyError(profile_id)
    if data.get("active_profile") == profile_id:
        data["active_profile"] = ""
    _save_store(data)
    HAProfileSecretStore(profile_id).clear()
    return {"removed": profile_id}


async def activate_profile(services: Any, profile_id: str) -> dict[str, Any]:
    from app.core.runtime_settings import save_runtime_settings

    data = _load_store()
    profile = next((p for p in data["profiles"] if p.get("profile_id") == profile_id), None)
    if profile is None:
        raise KeyError(profile_id)
    if not bool(profile.get("enabled")):
        raise PermissionError(f"Profile '{profile_id}' está desabilitado")
    url = str(profile.get("url") or "")
    if not url:
        raise ValueError(f"Profile '{profile_id}' não possui URL configurada")

    token = resolve_profile_token(profile_id, getattr(services, "settings", None))
    settings = services.settings
    setattr(settings, "home_assistant_url", url)
    save_runtime_settings({"home_assistant_url": url})

    controller = services.homelab
    client = getattr(controller, "home_assistant", None)
    if client is not None and hasattr(client, "set_credentials"):
        client.set_credentials(url, token)

    data["active_profile"] = profile_id
    _save_store(data)
    return {"active_profile": profile_id, "url": url,
            "auth_configured": bool(token),
            "runtime_applied": client is not None}


def set_profile_token(services: Any, profile_id: str, token: str) -> dict[str, Any]:
    public = get_profile(profile_id)  # valida existência
    store = HAProfileSecretStore(profile_id)
    if token.strip():
        store.save(token)
    else:
        store.clear()
    # Aplica imediatamente se o perfil estiver ativo (runtime real, §80).
    data = _load_store()
    if data.get("active_profile") == profile_id:
        client = getattr(getattr(services, "homelab", None), "home_assistant", None)
        if client is not None and hasattr(client, "set_credentials"):
            client.set_credentials(public["url"], resolve_profile_token(
                profile_id, getattr(services, "settings", None)))
    return {"profile_id": profile_id, "auth_configured": store.configured()}


# ---------------------------------------------------------------------------
# Test de conexão (§21/§82): API / Core version / state / entity count / latency
# Regressão §5/§8: UA identificado da KAZUMI em TODAS as chamadas; sem token,
# endpoints autenticados NÃO são contatados (estado UNCONFIGURED, §9).
# ---------------------------------------------------------------------------

def _probe_headers(token: str) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _probe(url: str, token: str, timeout: float = 6.0) -> dict[str, Any]:
    if not token:
        # §9: nenhuma chamada autenticada sai sem Bearer — nem para "verificar".
        return {"ok": False, "error_code": "HA_UNCONFIGURED"}
    try:
        require_secure_credential_transport(url)
    except IntegrationError as error:
        return {"ok": False, "error_code": error.code}
    headers = _probe_headers(token)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        root_response = await client.get(f"{url}/api/", headers=headers)
        if root_response.status_code in {401, 403}:
            return {"ok": False, "error_code": "HA_AUTH_FAILED",
                    "http_status": root_response.status_code}
        root_response.raise_for_status()
        config_response = await client.get(f"{url}/api/config", headers=headers)
        if config_response.status_code in {401, 403}:
            return {"ok": False, "error_code": "HA_AUTH_FAILED",
                    "http_status": config_response.status_code}
        config_response.raise_for_status()
        states_response = await client.get(f"{url}/api/states", headers=headers)
        if states_response.status_code in {401, 403}:
            return {"ok": False, "error_code": "HA_AUTH_FAILED",
                    "http_status": states_response.status_code}
        states_response.raise_for_status()
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    config_json = config_response.json() if config_response.status_code == 200 else {}
    states_json = states_response.json() if states_response.status_code == 200 else []
    return {
        "ok": True,
        "authenticated": True,
        "latency_ms": latency_ms,
        "api": "available",
        "core_version": str(config_json.get("version", ""))[:24],
        "state": str(config_json.get("state", ""))[:24],
        "location_name": str(config_json.get("location_name", ""))[:80],
        "entity_count": len(states_json) if isinstance(states_json, list) else 0,
    }


def _record_result(profile_id: str, result: dict[str, Any]) -> None:
    data = _load_store()
    for profile in data["profiles"]:
        if profile.get("profile_id") == profile_id:
            profile["last_test"] = {
                **result,
                "tested_at": time.time(),
            }
            if result.get("ok"):
                profile["status"] = "READY"
            elif result.get("error_code") == "HA_AUTH_FAILED":
                profile["status"] = "AUTH_FAILED"
            elif result.get("error_code") == "HA_UNCONFIGURED":
                profile["status"] = "UNCONFIGURED"
            else:
                profile["status"] = "OFFLINE"
            break
    _save_store(data)


async def test_profile(services: Any, profile_id: str) -> dict[str, Any]:
    public = get_profile(profile_id)
    if not public["enabled"]:
        raise PermissionError(
            f"Profile '{profile_id}' desabilitado — nenhum contato é feito com hardware inexistente."
        )
    if not public["url"]:
        raise ValueError(f"Profile '{profile_id}' sem URL configurada")
    token = resolve_profile_token(profile_id, getattr(services, "settings", None))
    if not token:
        result = {"ok": False, "error_code": "HA_UNCONFIGURED"}
        _record_result(profile_id, result)
        return {"profile_id": profile_id, **result}
    try:
        result = await _probe(public["url"], token)
    except httpx.HTTPStatusError as error:
        code = "HA_AUTH_FAILED" if error.response.status_code in {401, 403} else "HA_API_ERROR"
        result = {"ok": False, "error_code": code,
                  "http_status": error.response.status_code}
    except httpx.TimeoutException:
        result = {"ok": False, "error_code": "HA_TIMEOUT"}
    except httpx.HTTPError as error:
        code = "HA_TLS_ERROR" if isinstance(error, httpx.ConnectError) and "certificate" in str(error).casefold() \
            else "HA_OFFLINE"
        result = {"ok": False, "error_code": code, "error_type": type(error).__name__}
    _record_result(profile_id, result)
    return {"profile_id": profile_id, **result}


async def test_active_profile(services: Any) -> dict[str, Any]:
    active = active_profile_id()
    if not active:
        return {"ok": False, "error_code": "HA_NO_ACTIVE_PROFILE"}
    return await test_profile(services, str(active))


# ---------------------------------------------------------------------------
# Refresh do monitor (prompt11_2 §16-§19): o health loop do Homelab sonda a
# API HA autenticada a cada ciclo, mas antes NÃO atualizava last_success — o
# card ficava STALE ("Último sucesso há 954s") mesmo com a integração sadia.
# ---------------------------------------------------------------------------

_MONITOR_RECORD_COOLDOWN_SECONDS = 30.0
_last_monitor_record_monotonic = 0.0


def record_monitor_success(detail: dict[str, Any] | None = None,
                           base_url: str | None = None) -> bool:
    """Registra sucesso AUTENTICADO observado pelo monitor do Homelab.

    Atualiza ``last_test``/``last_success`` do perfil ativo (fonte única da
    UI) para que um HA saudável volte a READY em vez de STALE por cache antigo.
    Falhas de rede do monitor NÃO são registradas aqui: estados de falha
    continuam vindo de testes reais (§18). Nunca lança.
    """
    global _last_monitor_record_monotonic
    try:
        now = time.monotonic()
        if now - _last_monitor_record_monotonic < _MONITOR_RECORD_COOLDOWN_SECONDS:
            return False
        active = active_profile_id()
        if not active:
            return False
        data = _load_store()
        profile = next(
            (p for p in data["profiles"] if p.get("profile_id") == active), None
        )
        if not profile or not profile.get("enabled"):
            return False
        profile_url = str(profile.get("url") or "")
        if not profile_url:
            return False
        if base_url and profile_url.rstrip("/") != str(base_url).rstrip("/"):
            return False
        _record_result(active, {
            "ok": True,
            "authenticated": True,
            "source": "homelab_monitor",
            "core_version": str((detail or {}).get("version") or "")[:24],
            "state": str((detail or {}).get("state") or "")[:24],
        })
        _last_monitor_record_monotonic = now
        return True
    except Exception as error:  # noqa: BLE001 - refresh nunca derruba o monitor
        logger.warning("ha_monitor_record_failed type=%s", type(error).__name__)
        return False


# ---------------------------------------------------------------------------
# Fonte única de verdade para UI (Homelab + Integrations, prompt11_1 §44/§45):
# consolida profile + credential + último teste em um único snapshot coerente.
# ---------------------------------------------------------------------------

async def unified_ha_state(services: Any) -> dict[str, Any]:
    settings = getattr(services, "settings", None)
    enabled = bool(getattr(settings, "home_assistant_enabled", False))
    data = list_profiles()
    active = data.get("active_profile")
    profile = next(
        (p for p in data["profiles"] if p.get("profile_id") == active), None
    )
    if profile is None:
        profile = next(
            (p for p in data["profiles"]
             if p.get("enabled") and p.get("url")), None
        )
    url = str((profile or {}).get("url") or getattr(settings, "home_assistant_url", "") or "")
    last_test = (profile or {}).get("last_test") or {}
    # A persisted authenticated probe is literal evidence that credentials were
    # configured for that profile. Token removal clears the profile probe; this
    # fallback also keeps restart/UI state coherent when the broker is injected
    # after the public profile snapshot is assembled.
    auth_configured = (
        bool((profile or {}).get("auth_configured"))
        or bool(getattr(settings, "home_assistant_token", ""))
        or bool(last_test.get("ok") and last_test.get("authenticated"))
    )

    if not enabled:
        state, detail = "DISABLED", "Integração desabilitada"
    elif not url:
        state, detail = "UNCONFIGURED", "URL não configurada"
    elif not auth_configured:
        # Invariante §13: Auth Ausente NUNCA é READY/ONLINE.
        state, detail = "UNCONFIGURED", "Token ausente"
    else:
        tested_at = last_test.get("tested_at")
        age = (time.time() - float(tested_at)) if tested_at else None
        if last_test.get("ok"):
            if age is not None and age > _STALE_AFTER_SECONDS:
                state, detail = "STALE", f"Último sucesso há {int(age)}s"
            else:
                state, detail = "READY", "Teste autenticado bem-sucedido"
        elif last_test.get("error_code") == "HA_AUTH_FAILED":
            state, detail = "AUTH_FAILED", "Token recusado pelo Home Assistant"
        elif last_test.get("error_code") == "HA_UNCONFIGURED":
            state, detail = "UNCONFIGURED", "Token ausente"
        elif last_test:
            state, detail = "OFFLINE", f"Falha: {last_test.get('error_code') or 'desconhecida'}"
        else:
            state, detail = "STARTING", "Configurado; aguardando primeiro teste"

    authenticated_validated = bool(state == "READY" and last_test.get("ok"))
    return {
        "id": "home_assistant",
        "enabled": enabled,
        "configured": bool(url) and auth_configured,
        "url_configured": bool(url),
        "auth_configured": auth_configured,
        "authenticated": authenticated_validated,
        "state": state,
        "health": detail,
        "latency_ms": last_test.get("latency_ms"),
        "core_version": last_test.get("core_version"),
        "api_state": last_test.get("state"),
        "entity_count": last_test.get("entity_count"),
        "last_test": last_test.get("tested_at"),
        "last_success": last_test.get("tested_at") if last_test.get("ok") else None,
        "last_error": None if state in {"READY", "STARTING"} else detail,
        "active_profile": active,
        "open_url": url or None,
        "realtime_events": "NOT AVAILABLE",
    }


def apply_active_profile_to_runtime(services: Any) -> dict[str, Any]:
    """Restaura perfil ativo + credencial resolvível após restart (§59).

    Nunca lança: na inicialização uma falha de resolução não pode derrubar o
    backend; apenas mantém o client com a configuração legada anterior.
    """
    summary: dict[str, Any] = {"applied": False}
    try:
        active = active_profile_id()
        if not active:
            return summary
        data = _load_store()
        profile = next(
            (p for p in data["profiles"] if p.get("profile_id") == active), None
        )
        if not profile or not profile.get("enabled") or not profile.get("url"):
            return summary
        settings = getattr(services, "settings", None)
        token = resolve_profile_token(str(active), settings)
        url = str(profile["url"])
        if settings is not None:
            setattr(settings, "home_assistant_url", url)
        client = getattr(getattr(services, "homelab", None), "home_assistant", None)
        if client is not None and hasattr(client, "set_credentials"):
            # Nunca apaga credencial funcional: se nada foi resolvido e o
            # client já possui token, mantém o atual (§7).
            existing_token = ""
            try:
                existing_token = str(getattr(client, "bearer_token", "") or "")
            except Exception:  # noqa: BLE001
                pass
            if token or not existing_token:
                client.set_credentials(url, token)
        summary.update({"applied": True, "active_profile": str(active),
                        "auth_configured": bool(token)})
    except Exception as error:  # noqa: BLE001 - startup nunca falha por perfil HA
        logger.warning("ha_active_profile_restore_failed type=%s", type(error).__name__)
    return summary
