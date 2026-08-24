"""Windows host adapter.

V1 policy: Windows management is only attempted through a configured remote
method. Nothing is ever enabled automatically (no WinRM/TrustedHosts/firewall
changes without explicit operator authorization, spec §64). When no method is
configured the adapter reports CAPABILITY_UNAVAILABLE honestly and the host is
treated as a network-reachable machine only.
"""

from __future__ import annotations

import re
from typing import Any

from app.homelab.adapters.base import SshAdapterError, SshHostAdapter


class WindowsHostAdapter(SshHostAdapter):
    platform_label = "windows"
    remote_method = "unconfigured"

    def available(self) -> tuple[bool, str]:
        if self.remote_method == "ssh":
            return True, ""
        if self.remote_method == "winrm":
            return False, "WinRM ainda não está configurado para este host."
        if self.remote_method == "nyra_remote_node":
            return False, "NYRA Remote Node ainda não está implementado."
        return False, "Nenhum método de gerenciamento remoto configurado para este host Windows."

    async def metrics(self) -> dict[str, Any]:
        ok, reason = self.available()
        if not ok:
            raise SshAdapterError("CAPABILITY_UNAVAILABLE", reason)
        systeminfo_raw = await self.run("systeminfo", timeout_seconds=25, reason="homelab:windows:metrics")
        tasklist_raw = await self.run(
            "tasklist /FO CSV | findstr /C:\"python\" /C:\"nginx\"",
            timeout_seconds=15,
            reason="homelab:windows:metrics",
        )
        payload = self._parse_systeminfo(systeminfo_raw)
        payload["matching_processes"] = [
            line.split('","')[0].strip('"')[:80]
            for line in tasklist_raw.splitlines()
            if line.startswith('"')
        ][:20]
        return payload

    @staticmethod
    def _parse_systeminfo(raw: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for pattern, key in (
            (r"Host Name:\s*(\S+)", "hostname"),
            (r"OS Name:\s*(.+)", "os_name"),
            (r"System Boot Time:\s*(.+)", "boot_time"),
            (r"Total Physical Memory:\s*([\d,.]+)\s*MB", "total_memory_mb"),
            (r"Available Physical Memory:\s*([\d,.]+)\s*MB", "available_memory_mb"),
        ):
            match = re.search(pattern, raw or "", re.IGNORECASE)
            if match:
                value = match.group(1).strip()[:120]
                if key.endswith("_mb"):
                    try:
                        payload[key] = int(float(value.replace(".", "").replace(",", "")))
                    except ValueError:
                        continue
                else:
                    payload[key] = value
        total = payload.get("total_memory_mb")
        available = payload.get("available_memory_mb")
        if isinstance(total, int) and isinstance(available, int) and total:
            used = max(total - available, 0)
            payload["memory_percent"] = round(used * 100.0 / total, 1)
        return payload
