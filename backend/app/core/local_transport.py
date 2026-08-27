"""Trusted local HTTP/WebSocket boundary for NYRA's loopback API."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _host_name(value: str) -> str:
    raw = value.strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else ""
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


class LocalRequestSecurityMiddleware:
    """Reject DNS-rebinding and cross-site browser traffic before routing."""

    def __init__(self, app, *, frontend_port: int, backend_port: int) -> None:
        self.app = app
        self.allowed_hosts = set(_LOOPBACK_HOSTS)
        if _truthy(os.getenv("NYRA_TESTING")):
            self.allowed_hosts.add("testserver")
        self.allowed_origins = frozenset({
            f"http://127.0.0.1:{frontend_port}",
            f"http://localhost:{frontend_port}",
            f"http://127.0.0.1:{backend_port}",
            f"http://localhost:{backend_port}",
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
        })

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }

    def _reject_reason(self, scope) -> str | None:
        headers = self._headers(scope)
        if _host_name(headers.get("host", "")) not in self.allowed_hosts:
            return "untrusted_host"
        if headers.get("sec-fetch-site", "").lower() == "cross-site":
            return "cross_site_request"
        origin = headers.get("origin", "").rstrip("/")
        if origin:
            parsed = urlsplit(origin)
            if not parsed.scheme or not parsed.hostname or origin not in self.allowed_origins:
                return "untrusted_origin"
        return None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        reason = self._reject_reason(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return
        body = json.dumps({"detail": "Local request rejected", "error_code": reason}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
