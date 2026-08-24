"""Structured health/readiness checks: PROCESS, TCP, HTTP, COMMAND + external hooks."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import psutil

from app.runtime.models import HealthKind, HealthResult, ServiceSpec


async def run_health_check(spec: ServiceSpec, hooks: dict[str, object] | None = None) -> HealthResult | None:
    health = spec.health
    if health is None or health.kind == HealthKind.NONE:
        return None
    started = time.perf_counter()
    try:
        if health.kind == HealthKind.PROCESS:
            result = await _check_process(health.port or 0, health.process_match)
        elif health.kind == HealthKind.TCP:
            result = await _check_tcp(health.host, int(health.port or 0), health.timeout_seconds)
        elif health.kind == HealthKind.HTTP:
            result = await _check_http(health.url or "", health.expected_status, health.timeout_seconds)
        elif health.kind == HealthKind.COMMAND:
            result = await _check_command([str(part) for part in (health.command or [])], health.timeout_seconds)
        elif health.kind == HealthKind.WARM_MANAGER:
            result = await _check_hook(hooks or {}, "warm_manager")
        elif health.kind == HealthKind.SENTINEL:
            result = await _check_hook(hooks or {}, "sentinel")
        else:
            return None
    except (OSError, ValueError, RuntimeError):
        return HealthResult(healthy=False, detail="health check raised")
    result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return result


async def _check_process(port: int, match_tokens: list[str]) -> HealthResult:
    tokens = [token.casefold() for token in match_tokens]
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(process.info.get("cmdline") or []).casefold()
            name = str(process.info.get("name") or "").casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        haystack = f"{name} {cmdline}"
        if tokens and not any(token in haystack for token in tokens):
            continue
        if port and not _has_listening_port(process.info["pid"], port):
            continue
        return HealthResult(healthy=True, detail=f"process pid={process.info['pid']}")
    return HealthResult(healthy=False, detail="processo correspondente não encontrado")


def _has_listening_port(pid: int, port: int) -> bool:
    try:
        for connection in psutil.Process(pid).net_connections(kind="inet"):
            if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return False


async def _check_tcp(host: str, port: int, timeout_seconds: float) -> HealthResult:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_seconds,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError, socket.error):
            pass
        return HealthResult(healthy=True, detail=f"tcp {host}:{port} aceitou conexão")
    except (TimeoutError, ConnectionError, OSError):
        return HealthResult(healthy=False, detail=f"tcp {host}:{port} recusou ou expirou")


async def _check_http(url: str, expected_status: int, timeout_seconds: float) -> HealthResult:
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        return HealthResult(healthy=False, detail=f"http falhou: {type(exc).__name__}")
    healthy = response.status_code < 400 and (
        expected_status >= 300 or response.status_code == expected_status
    )
    detail = f"http {response.status_code}"
    if healthy and expected_status < 300:
        body = response.text[:200].casefold()
        if '"status": "degraded"' in body or '"status":"degraded"' in body:
            healthy = False
            detail += " corpo reporta degraded"
    return HealthResult(healthy=healthy, detail=detail)


async def _check_command(command: list[str], timeout_seconds: float) -> HealthResult:
    import subprocess as sp

    creationflags = sp.CREATE_NO_WINDOW if hasattr(sp, "CREATE_NO_WINDOW") else 0
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
            creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        return HealthResult(healthy=False, detail=f"command spawn falhou: {type(exc).__name__}")
    try:
        code = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        return HealthResult(healthy=False, detail="command health expirou")
    return HealthResult(healthy=code == 0, detail=f"exit code {code}")


async def _check_hook(hooks: dict[str, object], key: str) -> HealthResult:
    hook = hooks.get(key)
    if hook is None:
        return HealthResult(healthy=False, detail=f"hook '{key}' indisponível neste processo NYRA")
    getter = getattr(hook, "status", None) if not callable(hook) else hook
    try:
        value = getter() if asyncio.iscoroutinefunction(getter) is False else await getter()
        value = value() if callable(value) else value
    except Exception as exc:  # noqa: BLE001 - external component may raise anything
        return HealthResult(healthy=False, detail=f"{key} status erro: {type(exc).__name__}")
    state = str(value.get("state") or "") if isinstance(value, dict) else ""
    ready_markers = ("READY", "CONNECTED", "OLLAMA_READY", "ONLINE", "RUNNING")
    healthy = any(marker in state.upper() for marker in ready_markers) and "OFFLINE" not in state.upper() and "ERROR" not in state.upper()
    return HealthResult(healthy=healthy, detail=f"{key}={state or 'sem estado'}")
