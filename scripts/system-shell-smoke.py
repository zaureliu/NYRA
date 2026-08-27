from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.core.logging import configure_logging
from app.events import EventBus
from app.network_aliases import get_network_aliases
from app.tools.system_shell import SystemShellService


async def main() -> None:
    settings = Settings.from_sources()
    configure_logging(settings.log_level)
    shell = SystemShellService(settings, EventBus())
    await shell.initialize()
    aliases = get_network_aliases()
    gateway = aliases.resolve("gateway").address
    proxmox = aliases.resolve("proxmox").address
    dc1 = aliases.resolve("dc1").address
    commands = [
        ("ping_gateway", f"ping {gateway} -n 2 -w 1000", None),
        ("ping_proxmox", f"ping {proxmox} -n 2 -w 1000", None),
        ("ping_dc1", f"ping {dc1} -n 2 -w 1000", None),
        ("ping_internet", "ping 1.1.1.1 -n 2 -w 1000", None),
        ("ipconfig", "ipconfig", None),
        ("arp", "arp -a", None),
        ("route", "route print", None),
        ("processes", "Get-Process | Select-Object -First 5 Name,Id,CPU", None),
        ("services", "Get-Service | Select-Object -First 5 Name,Status", None),
        ("date", "Get-Date", None),
        ("location", "Get-Location", None),
        ("port_5173", "Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess,@{Name='Process';Expression={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}}", None),
        ("port_5173_raw", "Get-NetTCPConnection -LocalPort 5173 | Select-Object -Property LocalAddress,LocalPort,State,OwningProcess", None),
        ("net_adapters", "Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed", None),
        ("git_status", "git status --short", str(PROJECT_ROOT)),
        ("git_branch", "git branch --show-current", str(PROJECT_ROOT)),
        ("git_head", "git log -1 --oneline", str(PROJECT_ROOT)),
    ]
    if len(sys.argv) > 1:
        commands = [commands[int(index)] for index in sys.argv[1:]]
    output = []
    for name, command, cwd in commands:
        result = await shell.execute(
            command,
            timeout_seconds=15,
            working_directory=cwd,
            reason=f"safe smoke test: {name}",
        )
        output.append({
            "name": name,
            "success": result["success"],
            "exit_code": result["exit_code"],
            "risk_level": result["risk_level"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "error_code": result["error_code"],
            "stdout": result["stdout"][:4_000],
            "stderr": result["stderr"][:1_000],
        })
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
