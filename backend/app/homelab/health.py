"""Network Probe Layer for homelab hosts.

Reuses the validated probes from ``network_watch.targets`` (ICMP/TCP/DNS) and
adds an HTTP probe for arbitrary LAN URLs. Aggregation never treats a failed
ping as a definitive OFFLINE: ICMP may be blocked, so TCP/HTTP/integration
results correlate into the final state (spec §16, §138-142).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from app.homelab.models import HealthState, HostDefinition, ProbeResult
from app.network_watch.targets import icmp_probe as _icmp_probe
from app.network_watch.targets import tcp_probe as _tcp_probe


async def icmp_probe(host: str, timeout_seconds: float = 1.5) -> ProbeResult:
    try:
        ok, latency = await asyncio.wait_for(_icmp_probe(host, timeout_seconds), timeout_seconds + 2)
    except TimeoutError:
        return ProbeResult(kind="icmp", success=False, detail="timeout")
    return ProbeResult(
        kind="icmp",
        success=bool(ok),
        latency_ms=latency,
        detail="ok" if ok else "sem resposta ICMP",
    )


async def tcp_probe(host: str, port: int, timeout_seconds: float = 2.0) -> ProbeResult:
    ok, latency = await _tcp_probe(f"{host}:{int(port)}", timeout_seconds)
    return ProbeResult(
        kind="tcp",
        success=bool(ok),
        latency_ms=latency,
        detail=f"porta {port}",
    )


async def http_probe(url: str, timeout_seconds: float = 4.0, *, bearer_token: str = "") -> ProbeResult:
    started = time.perf_counter()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ProbeResult(kind="http", success=False, detail="URL inválida")
    headers = {"User-Agent": "NYRA-Homelab/1.0"}
    if bearer_token:
        # The token lives only in the request header; it never reaches
        # ProbeResult detail, logs or the registry (spec §94, §149-150).
        headers["Authorization"] = f"Bearer {bearer_token}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
        latency = round((time.perf_counter() - started) * 1000, 2)
        detail = f"HTTP {response.status_code}"
        # 401 means the HTTP stack is alive but auth is required — reachability yes.
        return ProbeResult(kind="http", success=response.status_code < 500, latency_ms=latency, detail=detail)
    except httpx.HTTPError as exc:
        return ProbeResult(kind="http", success=False, detail=f"{type(exc).__name__}")


def aggregate_state(
    host: HostDefinition,
    probes: list[ProbeResult],
    integration_state: HealthState,
    integration_error_code: str | None,
) -> tuple[HealthState, bool]:
    """Correlate probes + integration health into one normalized state.

    Returns (overall_state, reachable). Rules:
    - disabled host stays DISABLED;
    - any successful probe proves L2/L3/L7 reachability;
    - integration auth failures surface as AUTHENTICATION_FAILED (host is up);
    - integration unavailable on a reachable host is DEGRADED;
    - no successful probe at all yields UNREACHABLE (never invent OFFLINE).
    """
    if not host.enabled:
        return HealthState.DISABLED, False
    reachable = any(probe.success for probe in probes)
    if not reachable:
        return HealthState.UNREACHABLE, False
    if integration_state == HealthState.AUTHENTICATION_FAILED:
        return HealthState.AUTHENTICATION_FAILED, True
    if integration_state == HealthState.INTEGRATION_UNAVAILABLE:
        return HealthState.DEGRADED, True
    if integration_state == HealthState.OFFLINE:
        return HealthState.OFFLINE, True
    if host.capabilities.api or host.integration.value != "none":
        return HealthState.ONLINE, True
    return HealthState.ONLINE, True


class HomelabProbeLayer:
    """Runs bounded concurrent probes for one host according to its policy.

    ``credential_resolver`` may supply a bearer token per host (never stored
    here) so API reachability probes authenticate instead of generating auth
    failures on the target; hosts without a token fall back to credential-free
    endpoints (spec §103).
    """

    def __init__(
        self,
        default_timeout_seconds: float = 5.0,
        credential_resolver: Callable[[HostDefinition], str] | None = None,
    ) -> None:
        self.default_timeout = default_timeout_seconds
        self._credential_resolver = credential_resolver

    def _resolve_token(self, host: HostDefinition) -> str:
        if self._credential_resolver is None:
            return ""
        try:
            return str(self._credential_resolver(host) or "")
        except Exception:
            return ""

    async def probe_host(self, host: HostDefinition, timeout_seconds: float | None = None) -> list[ProbeResult]:
        timeout = float(timeout_seconds or self.default_timeout)
        tasks: list[asyncio.Task] = []
        caps = host.capabilities
        icmp_timeout = float(host.health_policy.get("icmp_timeout", min(timeout, 2.0)))
        if caps.icmp:
            tasks.append(asyncio.create_task(icmp_probe(host.address, icmp_timeout)))
        for port in caps.tcp_probes[:4]:
            tasks.append(asyncio.create_task(tcp_probe(host.address, port, min(timeout, 3.0))))
        if caps.http_path:
            token = self._resolve_token(host)
            if host.type.value == "proxmox":
                url = f"https://{host.address}:8006"
            else:
                url = f"http://{host.address}{caps.http_path}"
                # Without credentials an authenticated API path would only
                # accumulate auth failures on the target — probe the site root.
                if not token and host.integration.value == "home_assistant_api":
                    url = f"http://{host.address}/"
            tasks.append(asyncio.create_task(http_probe(url, min(timeout, 5.0), bearer_token=token)))
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cleaned: list[ProbeResult] = []
        for item in results:
            if isinstance(item, BaseException):
                cleaned.append(ProbeResult(kind="tcp", success=False, detail=type(item).__name__))
            elif isinstance(item, ProbeResult):
                cleaned.append(item)
        return cleaned
