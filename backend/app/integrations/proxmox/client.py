"""Proxmox integration via the native API (PVE API token auth).

Extends the original read-only client in place. GET helpers remain safe by
construction; VM lifecycle actions are separate explicit methods that only the
Homelab Control Plane calls after policy + approval. Every action returns an
UPID that MUST be awaited to completion and re-verified against guest state
before reporting success (spec §27-§34).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import ssl
import time
from pathlib import Path
from typing import Any

import httpx

from app.integrations.base import IntegrationError, require_secure_credential_transport


logger = logging.getLogger("nyra.homelab.proxmox")

_ERROR_PREFIX = "PROXMOX"
_DEFAULT_CA_RELATIVE_PATH = Path("NYRA") / "certs" / "proxmox-root-ca.pem"


class ProxmoxReadOnlyClient:
    """Proxmox VE API adapter. Reads are exposed directly; writes are explicit."""

    def __init__(
        self,
        base_url: str,
        token_id: str,
        token_secret: str,
        verify_ssl: bool = True,
        *,
        tls_fingerprint: str = "",
        timeout_seconds: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_id = token_id.strip()
        self._token_secret = token_secret
        self.verify_ssl = verify_ssl
        self.tls_fingerprint = tls_fingerprint.replace(":", "").replace(" ", "").casefold()
        self.timeout_seconds = timeout_seconds
        if not verify_ssl:
            logger.warning("proxmox_tls_verification_disabled")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token_id and self._token_secret)

    def set_credentials(
        self,
        base_url: str,
        token_id: str,
        token_secret: str,
        *,
        verify_ssl: bool | None = None,
        tls_fingerprint: str | None = None,
    ) -> None:
        """Troca runtime de credenciais/URL (usado pelo config da UI V11).

        O secret nunca é logado nem retornado; apenas substituído em memória.
        """
        self.base_url = (base_url or "").rstrip("/")
        self.token_id = (token_id or "").strip()
        self._token_secret = token_secret or ""
        if verify_ssl is not None:
            self.verify_ssl = bool(verify_ssl)
            if not self.verify_ssl:
                logger.warning("proxmox_tls_verification_disabled")
        if tls_fingerprint is not None:
            self.tls_fingerprint = tls_fingerprint.replace(":", "").replace(" ", "").casefold()

    # ------------------------------------------------------------------ plumbing

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"PVEAPIToken={self.token_id}={self._token_secret}"}

    def _httpx_verify(self) -> bool | ssl.SSLContext:
        """Keep TLS verification on, adding NYRA's private Proxmox CA when present."""
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            return True
        ca_file = Path(local_app_data) / _DEFAULT_CA_RELATIVE_PATH
        if not ca_file.is_file():
            return True
        return ssl.create_default_context(cafile=str(ca_file))

    async def _check_fingerprint(self) -> None:
        """Pin the server certificate leaf digest when a fingerprint is set.

        Implemented as a pre-flight TLS check with CERT_NONE followed by an
        exact sha256 comparison; mismatch aborts every API call.
        """
        if not self.tls_fingerprint:
            return
        parsed = httpx.URL(self.base_url)
        host, port = parsed.host or "", parsed.port or 8006
        loop = asyncio.get_running_loop()

        import socket

        def _fetch_cert() -> bytes:
            with socket.create_connection((host, port), timeout=5) as sock:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                    return tls_sock.getpeercert(binary_form=True) or b""

        try:
            der = await asyncio.wait_for(loop.run_in_executor(None, _fetch_cert), timeout=8)
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_TLS_UNAVAILABLE",
                "Não foi possível estabelecer TLS com o Proxmox para validar o fingerprint.",
            ) from exc
        digest = hashlib.sha256(der).hexdigest()
        if digest != self.tls_fingerprint:
            logger.warning("proxmox_tls_fingerprint_mismatch")
            raise IntegrationError(
                f"{_ERROR_PREFIX}_TLS_FINGERPRINT_MISMATCH",
                "O certificado do Proxmox não corresponde ao fingerprint cadastrado.",
            )

    async def _request(self, method: str, path: str, *, json_body: dict | None = None) -> Any:
        if not self.configured:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_AUTH_MISSING",
                "A integração Proxmox não está configurada; cadastre URL e API Token.",
            )
        if not self.verify_ssl:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_TLS_VERIFICATION_REQUIRED",
                "A API do Proxmox exige validação TLS ativa; instale a CA local no host NYRA.",
            )
        require_secure_credential_transport(self.base_url)
        await self._check_fingerprint()
        url = f"{self.base_url}/api2/json/{path.lstrip('/')}"
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    verify=self._httpx_verify(),
                ) as client:
                    response = await client.request(method, url, headers=self._headers(), json=json_body)
                break
            except (httpx.TimeoutException, httpx.TransportError, ssl.SSLError) as exc:
                last_error = exc
                if attempt == 1:
                    if _is_tls_verification_error(exc):
                        logger.warning("proxmox_tls_verification_failed")
                        raise IntegrationError(
                            f"{_ERROR_PREFIX}_TLS_ERROR",
                            "Falha de validação TLS: o certificado do Proxmox "
                            "(provavelmente self-signed) não é confiável com "
                            "verificação ativada.",
                        ) from exc
                    logger.warning("proxmox_api_unreachable", extra={"error_type": type(exc).__name__})
                    raise IntegrationError(
                        f"{_ERROR_PREFIX}_API_UNAVAILABLE",
                        "A API do Proxmox não respondeu dentro do timeout configurado.",
                    ) from exc
                await asyncio.sleep(0.4)
        assert response is not None
        if response.status_code == 401:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_AUTH_FAILED",
                "Não consegui autenticar na API do Proxmox (credencial recusada).",
            )
        if response.status_code == 403:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_PERMISSION_DENIED",
                "O token do Proxmox não possui permissão para esta operação.",
            )
        if response.status_code >= 400:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_API_ERROR",
                f"A API do Proxmox retornou HTTP {response.status_code}.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationError(
                f"{_ERROR_PREFIX}_API_INVALID_RESPONSE",
                "A API do Proxmox devolveu uma resposta não-JSON.",
            ) from exc
        return payload.get("data")

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, body: dict | None = None) -> Any:
        return await self._request("POST", path, json_body=body or {})

    # ------------------------------------------------------------------ reads

    async def version(self) -> dict[str, Any]:
        data = await self._get("version")
        return data if isinstance(data, dict) else {}

    async def nodes(self) -> list[dict[str, Any]]:
        data = await self._get("nodes")
        return data if isinstance(data, list) else []

    async def node_status(self, node: str) -> dict[str, Any]:
        data = await self._get(f"nodes/{node}/status")
        return data if isinstance(data, dict) else {}

    async def cluster_status(self) -> list[dict[str, Any]]:
        data = await self._get("cluster/status")
        return data if isinstance(data, list) else []

    async def virtual_machines(self) -> list[dict[str, Any]]:
        data = await self._get("cluster/resources?type=vm")
        return data if isinstance(data, list) else []

    async def storage(self) -> list[dict[str, Any]]:
        data = await self._get("cluster/resources?type=storage")
        return data if isinstance(data, list) else []

    async def guest_config(self, node: str, guest_type: str, vmid: int) -> dict[str, Any]:
        data = await self._get(f"nodes/{node}/{guest_type}/{int(vmid)}/config")
        return data if isinstance(data, dict) else {}

    async def guest_status(self, node: str, guest_type: str, vmid: int) -> dict[str, Any]:
        base = f"nodes/{node}/{'qemu' if guest_type == 'qemu' else 'lxc'}/{int(vmid)}"
        if guest_type == "qemu":
            status = await self._get(f"{base}/status/current")
        else:
            status = await self._get(f"{base}/status")
        return status if isinstance(status, dict) else {}

    async def recent_tasks(self, node: str, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._get(f"nodes/{node}/tasks?limit={int(limit)}")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ tasks / grounding

    async def task_status(self, node: str, upid: str) -> dict[str, Any]:
        safe_upid = httpx.QueryParams({"upid": upid}).get("upid", "")
        if not upid.startswith("UPID:") or safe_upid != upid:
            raise IntegrationError(f"{_ERROR_PREFIX}_TASK_FAILED", "UPID inválido.")
        data = await self._get(f"nodes/{node}/tasks/{_encode_path_segment(upid)}/status")
        return data if isinstance(data, dict) else {}

    async def wait_task(
        self,
        node: str,
        upid: str,
        *,
        timeout_seconds: float = 60.0,
        poll_interval: float = 0.6,
    ) -> dict[str, Any]:
        """Wait until a PVE task reaches `stopped`; never trust the UPID alone."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = await self.task_status(node, upid)
            state = str(status.get("status") or "")
            if state == "stopped":
                exitstatus = str(status.get("exitstatus") or "")
                if exitstatus == "OK":
                    return {"state": state, "exitstatus": exitstatus, "ok": True}
                logger.warning("proxmox_task_failed", extra={"exitstatus": exitstatus[:64]})
                return {"state": state, "exitstatus": exitstatus, "ok": False}
            if time.monotonic() > deadline:
                return {"state": state or "running", "exitstatus": "", "ok": False, "timed_out": True}
            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------ actions (explicit)

    async def _guest_action_endpoint(self, guest_type: str, action: str) -> str:
        if guest_type == "qemu":
            endpoints = {
                "start": "start", "shutdown": "shutdown", "stop": "stop",
                "reboot": "reboot", "reset": "reset",
            }
        elif guest_type == "lxc":
            endpoints = {
                "start": "start", "shutdown": "shutdown", "stop": "stop",
                "reboot": "reboot",
            }
        else:
            raise IntegrationError(f"{_ERROR_PREFIX}_VM_NOT_FOUND", f"Tipo de guest inválido: {guest_type}")
        return endpoints[action]

    async def guest_action(
        self,
        node: str,
        guest_type: str,
        vmid: int,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> str:
        endpoint = await self._guest_action_endpoint(guest_type, action)
        body = dict(extra or {})
        if guest_type == "lxc" and action == "shutdown":
            body.setdefault("timeout", 60)
        data = await self._post(f"nodes/{node}/{guest_type}/{int(vmid)}/{endpoint}", body)
        upid = ""
        if isinstance(data, str):
            upid = data
        elif isinstance(data, dict):
            upid = str(data.get("upid") or "")
        if not upid.startswith("UPID:"):
            raise IntegrationError(
                f"{_ERROR_PREFIX}_API_ERROR",
                "A API do Proxmox não retornou um UPID de tarefa.",
            )
        return upid


def _encode_path_segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _is_tls_verification_error(exc: BaseException) -> bool:
    """Detecta falha de VALIDAÇÃO de certificado (prompt11_2 §15).

    Self-signed com verify_ssl=True NÃO é AUTH_FAILED nem indisponibilidade:
    vira ``PROXMOX_TLS_ERROR`` e a verificação nunca é desligada sozinha.
    """
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            return False
        if isinstance(current, ssl.SSLError):
            return True
        text = str(current).casefold()
        if "certificate verify failed" in text or "certificate_verify_failed" in text:
            return True
        current = current.__cause__ or current.__context__
    return False
