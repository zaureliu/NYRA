from __future__ import annotations

import asyncio
import ipaddress
import platform
import re
import socket
import time
from dataclasses import dataclass
from typing import Any

import httpx
import psutil


@dataclass(frozen=True)
class DefaultRoute:
    gateway: str | None
    interface_ip: str | None
    interface_name: str | None


@dataclass(frozen=True)
class NetworkCounterRates:
    rx_bytes_per_sec: float | None = None
    tx_bytes_per_sec: float | None = None
    rx_packets_per_sec: float | None = None
    tx_packets_per_sec: float | None = None
    errors_rx_delta: int | None = None
    errors_tx_delta: int | None = None
    drops_rx_delta: int | None = None
    drops_tx_delta: int | None = None


def calculate_counter_rates(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    elapsed_seconds: float,
) -> NetworkCounterRates:
    """Calculate real interface deltas, resetting cleanly on switches/resets."""
    if not previous or previous.get("name") != current.get("name") or elapsed_seconds <= 0:
        return NetworkCounterRates()
    fields = (
        "bytes_received", "bytes_sent", "packets_received", "packets_sent",
        "errors_received", "errors_sent", "drops_received", "drops_sent",
    )
    if any(previous.get(field) is None or current.get(field) is None for field in fields):
        return NetworkCounterRates()
    deltas = {field: int(current[field]) - int(previous[field]) for field in fields}
    if any(value < 0 for value in deltas.values()):
        return NetworkCounterRates()
    return NetworkCounterRates(
        rx_bytes_per_sec=round(deltas["bytes_received"] / elapsed_seconds, 2),
        tx_bytes_per_sec=round(deltas["bytes_sent"] / elapsed_seconds, 2),
        rx_packets_per_sec=round(deltas["packets_received"] / elapsed_seconds, 2),
        tx_packets_per_sec=round(deltas["packets_sent"] / elapsed_seconds, 2),
        errors_rx_delta=deltas["errors_received"],
        errors_tx_delta=deltas["errors_sent"],
        drops_rx_delta=deltas["drops_received"],
        drops_tx_delta=deltas["drops_sent"],
    )


async def detect_default_route() -> DefaultRoute:
    if platform.system() != "Windows":
        return await asyncio.to_thread(_fallback_route)
    process = await asyncio.create_subprocess_exec(
        "route", "print", "-4", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3)
    text = stdout.decode(errors="replace")
    candidates: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        match = re.match(r"\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+(\S+)\s+(\d+)\s*$", line)
        if match:
            candidates.append((int(match.group(3)), match.group(1), match.group(2)))
    if not candidates:
        return await asyncio.to_thread(_fallback_route)
    _, gateway, interface_ip = min(candidates)
    return DefaultRoute(gateway, interface_ip, _interface_for_ip(interface_ip))


def _fallback_route() -> DefaultRoute:
    # UDP connect discovers the selected local route without sending application data.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("1.1.1.1", 53))
            interface_ip = probe.getsockname()[0]
        except OSError:
            interface_ip = None
    return DefaultRoute(None, interface_ip, _interface_for_ip(interface_ip))


def _interface_for_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    for name, addresses in psutil.net_if_addrs().items():
        if any(address.family == socket.AF_INET and address.address == ip for address in addresses):
            return name
    return None


async def icmp_probe(host: str, timeout_seconds: float = 1.5) -> tuple[bool, float | None]:
    # Fixed executable and validated IP/hostname only; no shell or user-controlled flags.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            return False, None
    args = ("ping", "-n", "1", "-w", str(int(timeout_seconds * 1000)), host) if platform.system() == "Windows" else ("ping", "-c", "1", "-W", str(max(1, int(timeout_seconds))), host)
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout_seconds + 1)
    except TimeoutError:
        process.kill()
        await process.wait()
        return False, None
    if process.returncode != 0:
        return False, None
    text = stdout.decode(errors="replace")
    match = re.search(r"(?:tempo|time)[=<]?\s*(\d+(?:[.,]\d+)?)\s*ms", text, re.IGNORECASE)
    latency = float(match.group(1).replace(",", ".")) if match else (time.perf_counter() - started) * 1000
    return True, round(latency, 2)


async def tcp_probe(target: str, timeout_seconds: float = 2) -> tuple[bool, float | None]:
    host, _, port_text = target.rpartition(":")
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port_text)), timeout_seconds)
        writer.close()
        await writer.wait_closed()
        return True, round((time.perf_counter() - started) * 1000, 2)
    except (OSError, TimeoutError, ValueError):
        return False, None


async def dns_probe(host: str, timeout_seconds: float = 3) -> tuple[bool, float | None]:
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout_seconds)
        return True, round((time.perf_counter() - started) * 1000, 2)
    except (OSError, TimeoutError):
        return False, None


async def http_probe(timeout_seconds: float = 4) -> tuple[bool, float | None]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(
                "http://www.msftconnecttest.com/connecttest.txt",
                headers={"User-Agent": "NYRA-Network-Watch/3.3"},
            )
        return response.status_code < 500, round((time.perf_counter() - started) * 1000, 2)
    except httpx.HTTPError:
        return False, None


def interface_counters(name: str | None) -> dict[str, Any]:
    if not name:
        return {"name": None, "up": None}
    stats = psutil.net_if_stats().get(name)
    counters = psutil.net_io_counters(pernic=True).get(name)
    addresses = psutil.net_if_addrs().get(name, [])
    ipv4 = next((item.address for item in addresses if item.family == socket.AF_INET), None)
    ipv6 = next(
        (item.address.split("%", 1)[0] for item in addresses
         if item.family == socket.AF_INET6 and not item.address.lower().startswith("fe80:")),
        None,
    )
    normalized = name.casefold()
    interface_type = (
        "wi-fi" if any(token in normalized for token in ("wi-fi", "wifi", "wireless", "wlan"))
        else "ethernet" if any(token in normalized for token in ("ethernet", "lan"))
        else None
    )
    return {
        "name": name,
        "type": interface_type,
        "up": bool(stats.isup) if stats else None,
        "ip_address": ipv4,
        "ipv6_address": ipv6,
        "link_speed_mbps": float(stats.speed) if stats and stats.speed > 0 else None,
        "mtu": int(stats.mtu) if stats and stats.mtu > 0 else None,
        "bytes_sent": counters.bytes_sent if counters else None,
        "bytes_received": counters.bytes_recv if counters else None,
        "packets_sent": counters.packets_sent if counters else None,
        "packets_received": counters.packets_recv if counters else None,
        "errors_sent": counters.errout if counters else None,
        "errors_received": counters.errin if counters else None,
        "drops_sent": counters.dropout if counters else None,
        "drops_received": counters.dropin if counters else None,
    }
