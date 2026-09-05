"""Real homelab smoke for the KAZUMI Control Plane.

Runs read-only checks against the operator's actual infrastructure using the
configured .env (never printing secrets):
  - network probes per registry host;
  - Home Assistant REST API (when token configured);
  - Proxmox API (when token configured; otherwise reports AUTH_MISSING honestly);
  - OpenWrt structured status over Trusted SSH (read-only only);
  - full overview with timing.

Usage:
  python scripts/homelab-smoke.py [--only ha|proxmox|openwrt|overview|hosts]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.homelab.controller import HomelabControlPlane  # noqa: E402
from app.homelab.history import HomelabHistory  # noqa: E402
from app.homelab.registry import HomelabHostRegistry  # noqa: E402
from app.integrations.base import IntegrationError  # noqa: E402
from app.tools.shell_approval import ShellApprovalGate  # noqa: E402
from app.tools.remote_shell import RemoteShellService  # noqa: E402


def line(label: str, value: object = "") -> None:
    print(f"  {label:<28} {value}")


async def smoke_hosts(plane: HomelabControlPlane) -> None:
    print("\n== HOSTS (network probe + integration health) ==")
    for host in plane.registry.all_hosts():
        started = time.perf_counter()
        try:
            health = await plane.host_status(host.id, force=True)
            probes = ", ".join(f"{p.kind}={'ok' if p.success else 'fail'}" for p in health.probes)
            line(host.id, f"{health.overall_state.value} reachable={health.reachable} ({time.perf_counter() - started:.2f}s)")
            line("  probes", probes or "n/a")
            if health.integration_error_code:
                line("  integration", f"{health.integration_error_code}")
        except IntegrationError as exc:
            line(host.id, f"{exc.code}: {exc.message}")


async def smoke_ha(plane: HomelabControlPlane) -> None:
    print("\n== HOME ASSISTANT (REST real) ==")
    result = await plane.ha_status()
    for key in ("enabled", "configured", "api_response", "location_name", "state", "version", "time_zone", "entity_count"):
        if key in result:
            line(key, result[key])
    if result.get("error_code"):
        line("error_code", f"{result['error_code']}: {result.get('message', '')}")


async def smoke_proxmox(plane: HomelabControlPlane) -> None:
    print("\n== PROXMOX (API nativa) ==")
    if not plane.proxmox.configured:
        line("status", "PROXMOX_AUTH_MISSING — configure KAZUMI_PROXMOX_TOKEN_ID/SECRET (docs/integrations/proxmox.md)")
        return
    try:
        version = await plane.proxmox.version()
        line("version", version.get("version"))
        nodes = await plane.proxmox.nodes()
        line("nodes", [n.get("node") for n in nodes])
        guests = await plane.proxmox.virtual_machines()
        running = [g for g in guests if g.get("status") == "running"]
        line("guests", f"{len(guests)} total / {len(running)} running")
        for guest in guests[:12]:
            line(f"  [{guest.get('type')}] {guest.get('vmid')}", f"{guest.get('name')} = {guest.get('status')}")
        storages = await plane.proxmox.storage()
        line("storages", len(storages))
    except IntegrationError as exc:
        line("error", f"{exc.code}: {exc.message}")


async def smoke_openwrt(plane: HomelabControlPlane) -> None:
    print("\n== OPENWRT (Trusted SSH, somente leitura) ==")
    try:
        status = await plane.openwrt_status()
        release = (status.get("release") or {}).get("DISTRIB_RELEASE", "?")
        line("release", release)
        line("uptime_s", status.get("uptime_s"))
        wan = status.get("wan") or {}
        line("wan", f"{wan.get('interface')} up={wan.get('up')} addr={','.join(wan.get('addresses') or [])}")
        route = status.get("default_route")
        line("default_route", route)
    except IntegrationError as exc:
        line("error", f"{exc.code}: {exc.message}")


async def smoke_overview(plane: HomelabControlPlane) -> None:
    print("\n== OVERVIEW ('verifica meu homelab') ==")
    started = time.perf_counter()
    overview = await plane.overview(force=True)
    elapsed = time.perf_counter() - started
    started_cached = time.perf_counter()
    await plane.overview()
    cached_elapsed = time.perf_counter() - started_cached
    for item in overview.hosts:
        line(item.host_id, f"{item.overall_state.value} (integration={item.integration_state.value})")
    line("summary", json.dumps(overview.summary, ensure_ascii=False))
    line("cold overview", f"{elapsed:.2f}s")
    line("cached overview", f"{cached_elapsed * 1000:.1f}ms")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["ha", "proxmox", "openwrt", "overview", "hosts"], default=None)
    args = parser.parse_args()

    settings = get_settings()
    print(f"homelab_enabled={settings.homelab_enabled} ha_url={settings.home_assistant_url or '-'} "
          f"proxmox_url={settings.proxmox_url or '-'} ha_token={'SET' if settings.home_assistant_token else 'MISSING'} "
          f"proxmox_token={'SET' if settings.proxmox_token_secret else 'MISSING'}")

    bus = EventBus()
    approvals = ShellApprovalGate()
    remote_shell = RemoteShellService(settings, bus, approvals)
    await remote_shell.initialize()
    history = HomelabHistory(settings.database_path)
    await history.initialize()
    plane = HomelabControlPlane(
        settings, bus, approvals, remote_shell,
        history=history,
        registry=HomelabHostRegistry(path=settings.homelab_registry_path),
    )
    await plane.history.initialize()

    sections = {
        "hosts": smoke_hosts,
        "ha": smoke_ha,
        "proxmox": smoke_proxmox,
        "openwrt": smoke_openwrt,
        "overview": smoke_overview,
    }
    selected = [args.only] if args.only else list(sections)
    for name in selected:
        try:
            await sections[name](plane)
        except IntegrationError as exc:
            line("error", f"{exc.code}: {exc.message}")
        except Exception as exc:
            line("error", f"{type(exc).__name__}: {exc}")
    print("\nSMOKE DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
