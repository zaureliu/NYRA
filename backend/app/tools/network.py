from __future__ import annotations

import asyncio
import platform
import socket
import time
from typing import Any

import httpx
import psutil


async def ping_host(host: str, timeout_seconds: float) -> dict[str, Any]:
    if platform.system() == "Windows":
        args = ("ping", "-n", "1", "-w", str(int(timeout_seconds * 1000)), host)
    else:
        args = ("ping", "-c", "1", "-W", str(max(1, int(timeout_seconds))), host)
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds + 1)
    except TimeoutError:
        process.kill()
        await process.wait()
        return {"host": host, "reachable": False, "reason": "timeout"}
    return {
        "host": host,
        "reachable": process.returncode == 0,
        "return_code": process.returncode,
        "summary": (stdout or stderr).decode(errors="replace")[-500:],
    }


async def dns_lookup(host: str, timeout_seconds: float) -> dict[str, Any]:
    async def resolve() -> list[str]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return sorted({record[4][0] for record in records})

    addresses = await asyncio.wait_for(resolve(), timeout_seconds)
    return {"host": host, "addresses": addresses}


async def tcp_port_check(host: str, port: int, timeout_seconds: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout_seconds
        )
        writer.close()
        await writer.wait_closed()
        return {
            "host": host,
            "port": port,
            "open": True,
            "connect_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except (OSError, TimeoutError) as exc:
        return {"host": host, "port": port, "open": False, "reason": type(exc).__name__}


async def get_local_system_stats() -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_percent": memory.percent,
            "memory_total_gb": round(memory.total / 1024**3, 2),
            "memory_available_gb": round(memory.available / 1024**3, 2),
            "disk_percent": disk.percent,
            "disk_total_gb": round(disk.total / 1024**3, 2),
            "disk_free_gb": round(disk.free / 1024**3, 2),
            "uptime_seconds": int(time.time() - psutil.boot_time()),
        }

    return await asyncio.to_thread(collect)


async def get_network_interfaces() -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        stats = psutil.net_if_stats()
        output: list[dict[str, Any]] = []
        for name, addresses in psutil.net_if_addrs().items():
            output.append(
                {
                    "name": name,
                    "up": stats.get(name).isup if name in stats else None,
                    "speed_mbps": stats.get(name).speed if name in stats else None,
                    "addresses": [
                        {"family": str(address.family), "address": address.address}
                        for address in addresses
                        if address.family in {socket.AF_INET, socket.AF_INET6}
                    ],
                }
            )
        return {"interfaces": output}

    return await asyncio.to_thread(collect)


async def check_http_service(url: str, timeout_seconds: float) -> dict[str, Any]:
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers={"User-Agent": "KAZUMI-Homelab-Monitor/0.1"})
            return {
                "url": url,
                "online": response.status_code < 500,
                "status_code": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "content_type": response.headers.get("content-type", ""),
            }
        except httpx.HTTPError as exc:
            return {"url": url, "online": False, "reason": type(exc).__name__}

