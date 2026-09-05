from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.events import EventBus
from app.tools.remote_shell import RemoteShellService
from app.tools.system_shell import SystemShellService


CHECKS = [
    ("proxmox_hostname", "remote", "proxmox", "hostname"),
    ("proxmox_uptime", "remote", "proxmox", "uptime"),
    ("proxmox_memory", "remote", "proxmox", "free -h"),
    ("proxmox_storage", "remote", "proxmox", "df -h"),
    ("proxmox_version", "remote", "proxmox", "pveversion"),
    ("proxmox_vms", "remote", "proxmox", "qm list"),
    ("openwrt_uptime", "remote", "openwrt", "uptime"),
    ("openwrt_addresses", "remote", "openwrt", "ip addr"),
    ("openwrt_routes", "remote", "openwrt", "ip route"),
    ("openwrt_logs", "remote", "openwrt", "logread | tail -n 40"),
]


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kazumi-remote-smoke-") as directory:
        settings = Settings.from_sources(database_path=Path(directory) / "smoke.db")
        bus = EventBus(history_size=500)
        local = SystemShellService(settings, bus)
        await local.initialize()
        remote = RemoteShellService(settings, bus, local.approvals)
        await remote.initialize()
        selected = CHECKS if len(sys.argv) == 1 else [CHECKS[int(index)] for index in sys.argv[1:]]
        results = []
        for name, kind, host, command in selected:
            if kind == "local":
                value = await local.execute(command, shell="cmd", reason="safe remote smoke connectivity")
            else:
                value = await remote.execute(host, command, reason="safe read-only remote smoke")
            results.append({
                "name": name, "kind": kind, "host": value.get("host", host),
                "success": value.get("success"), "error_code": value.get("error_code"),
                "exit_code": value.get("exit_code"), "risk_level": value.get("risk_level"),
                "duration_ms": value.get("duration_ms"), "timed_out": value.get("timed_out"),
                "message": value.get("message"), "stdout": str(value.get("stdout", ""))[:2000],
                "stderr": str(value.get("stderr", ""))[:1000],
            })
        print(json.dumps({"remote_status": remote.status(), "checks": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
