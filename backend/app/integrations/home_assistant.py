"""Home Assistant REST API integration.

Base URL comes from configuration exactly as provided; a port is never
auto-appended.
The long-lived access token lives only in settings/env and is sent exclusively
as an Authorization header — it never reaches logs, the registry or the LLM.
"""

from __future__ import annotations

import logging
from typing import Any
import httpx

from app.integrations.base import IntegrationError, require_secure_credential_transport


logger = logging.getLogger("nyra.homelab.ha")

_ERROR_PREFIX = "HA"
_MAX_STATES = 500
_USER_AGENT = "NYRA-Homelab/1.0"

# Endpoints que o Home Assistant protege por autenticação. A NYRA NUNCA deve
# contatá-los sem Bearer: além do 401, cada tentativa registra um evento de
# "invalid authentication" no log do Home Assistant (regressão prompt11_1 §4-§9).
_AUTHENTICATED_PREFIXES = ("/api/", "/api/config", "/api/states", "/api/services/")


def requires_authentication(path: str) -> bool:
    normalized = f"/{path.lstrip('/')}"
    return normalized.startswith(_AUTHENTICATED_PREFIXES) or normalized == "/api"


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        enabled: bool = True,
        timeout_seconds: float = 6.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._token = token
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds

    def set_credentials(self, base_url: str, token: str) -> None:
        """Troca runtime de URL/token (usado pela troca de profile da UI V3).

        O token nunca é logado nem exposto; apenas substituído em memória.
        """
        self.base_url = (base_url or "").rstrip("/")
        self._token = token

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self._token)

    @property
    def auth_missing(self) -> bool:
        return bool(self.enabled and self.base_url and not self._token)

    @property
    def bearer_token(self) -> str:
        """Token shared only with in-process reachability probes.

        Never logged, persisted or exposed to the LLM/frontend (spec §94,
        §149-150); consumers must use it exclusively as a request header.
        """
        return self._token

    # ------------------------------------------------------------------ plumbing

    async def _request(self, method: str, path: str, *, json_body: Any | None = None) -> httpx.Response:
        if not self.base_url:
            raise IntegrationUnavailable("A integração Home Assistant não possui URL configurada.")
        if not self._token and requires_authentication(path):
            # Guarda de regressão: sem token, nenhum request sai para endpoint
            # autenticado — o chamador recebe UNCONFIGURED em vez de gerar um
            # evento "invalid authentication" no Home Assistant (§8/§9).
            logger.info("ha_request_blocked_unauthenticated", extra={"path": path[:64]})
            raise IntegrationError(
                f"{_ERROR_PREFIX}_AUTH_MISSING",
                "Token de acesso do Home Assistant não configurado.",
            )
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"User-Agent": _USER_AGENT}
        if self._token:
            require_secure_credential_transport(self.base_url)
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                return await client.request(method, url, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            logger.warning("ha_api_timeout", extra={"path": path[:64]})
            raise IntegrationError("HA_API_UNAVAILABLE", "A API do Home Assistant não respondeu a tempo.") from exc
        except httpx.HTTPError as exc:
            logger.info("ha_api_unreachable", extra={"error_type": type(exc).__name__})
            raise IntegrationError("HA_API_UNAVAILABLE", "A API do Home Assistant está inacessível.") from exc

    def _raise_for(self, response: httpx.Response, path: str = "") -> None:
        if response.status_code in {401, 403}:
            raise IntegrationError("HA_AUTH_FAILED", "Não consegui autenticar na API do Home Assistant.")
        if response.status_code == 404:
            raise IntegrationError("HA_ENTITY_NOT_FOUND", "Entidade ou endpoint não encontrado no Home Assistant.")
        if response.status_code >= 400:
            raise IntegrationError(
                "HA_SERVICE_FAILED" if "/api/services/" in path else "HA_API_UNAVAILABLE",
                f"O Home Assistant respondeu HTTP {response.status_code}.",
            )

    # ------------------------------------------------------------------ reads

    async def api_root(self) -> str:
        response = await self._request("GET", "/api/")
        self._raise_for(response)
        return response.text.strip()[:200]

    async def config(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/config")
        self._raise_for(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationError("HA_API_UNAVAILABLE", "Resposta não-JSON da API do Home Assistant.") from exc
        return payload if isinstance(payload, dict) else {}

    async def states(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/states")
        self._raise_for(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationError("HA_API_UNAVAILABLE", "Resposta não-JSON da API do Home Assistant.") from exc
        return payload[:_MAX_STATES] if isinstance(payload, list) else []

    async def state(self, entity_id: str) -> dict[str, Any]:
        safe_entity = entity_id.strip()
        if not safe_entity or any(ch in safe_entity for ch in "\r\n?#/"):
            raise IntegrationError("HA_ENTITY_NOT_FOUND", "entity_id inválido.")
        response = await self._request("GET", f"/api/states/{safe_entity}")
        self._raise_for(response, f"/api/states/{safe_entity}")
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ actions

    async def call_service(
        self,
        domain: str,
        service: str,
        target: dict[str, Any] | None = None,
        service_data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {}
        if target:
            body["target"] = _safe_target(target)
        if service_data:
            body["service_data"] = service_data
        response = await self._request("POST", f"/api/services/{domain}/{service}", json_body=body)
        self._raise_for(response, f"/api/services/{domain}/{service}")
        payload = response.json()
        return payload if isinstance(payload, list) else []

    async def verify_effect(self, entity_id: str, expected_state: str) -> bool:
        """ACT→VERIFY: a 200 from /api/services is acceptance, never the effect."""
        try:
            current = await self.state(entity_id)
        except IntegrationError:
            return False
        return str(current.get("state") or "").casefold() == expected_state.strip().casefold()


class IntegrationUnavailable(IntegrationError):
    def __init__(self, message: str) -> None:
        super().__init__("HA_AUTH_MISSING", message)


def _safe_target(target: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}
    cleaned: dict[str, Any] = {}
    for key, value in target.items():
        if key not in allowed_keys:
            continue
        if isinstance(value, list):
            cleaned[key] = [str(item)[:120] for item in value if str(item).strip()][:32]
        elif isinstance(value, str) and value.strip():
            cleaned[key] = value.strip()[:120]
    if not cleaned:
        raise IntegrationError("HA_SERVICE_FAILED", "Target inválido para service call.")
    for values in cleaned.values():
        items = values if isinstance(values, list) else [values]
        for item in items:
            if any(ch in item for ch in "\r\n"):
                raise IntegrationError("HA_SERVICE_FAILED", "Target contém caracteres inválidos.")
    return cleaned
