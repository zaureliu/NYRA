"""Linux host adapter over Trusted SSH: normalized CPU/load, memory, storage."""

from __future__ import annotations

import re
from typing import Any

from app.homelab.adapters.base import SshHostAdapter, to_float, to_int


class LinuxHostAdapter(SshHostAdapter):
    platform_label = "linux"

    async def metrics(self) -> dict[str, Any]:
        proc_raw = await self.run(
            "cat /proc/uptime /proc/loadavg /proc/meminfo",
            reason="homelab:linux:metrics",
        )
        df_raw = await self.run(
            "df -kP -l",
            timeout_seconds=12,
            reason="homelab:linux:metrics",
        )
        return {
            **self._parse_proc(proc_raw),
            "filesystems": self._parse_df(df_raw),
        }

    async def services(self, limit: int = 50) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 120))
        raw = await self.run(
            f"systemctl list-units --type=service --no-legend --no-pager | head -n {bounded}",
            timeout_seconds=12,
            reason="homelab:linux:services",
        )
        units = []
        for line in raw.splitlines()[:bounded]:
            parts = line.split(None, 4)
            if len(parts) >= 4:
                units.append({
                    "unit": parts[0][:80],
                    "load": parts[1][:16],
                    "active": parts[2][:16],
                    "description": (parts[4] if len(parts) > 4 else "")[:120],
                })
        return {"units": units}

    async def logs(self, lines: int = 30) -> dict[str, Any]:
        bounded = max(1, min(int(lines), 120))
        raw = await self.run(
            f"journalctl -n {bounded} --no-pager",
            timeout_seconds=12,
            reason="homelab:linux:logs",
        )
        return {"lines": [line[:400] for line in raw.splitlines()[-bounded:]]}

    @staticmethod
    def _parse_proc(raw: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        lines = raw.splitlines()
        if lines:
            uptime_parts = lines[0].split()
            if uptime_parts:
                payload["uptime_s"] = to_float(uptime_parts[0])
        if len(lines) > 1:
            load_parts = lines[1].split()
            if load_parts:
                payload["load"] = {
                    "one": to_float(load_parts[0]),
                    "five": to_float(load_parts[1]),
                    "fifteen": to_float(load_parts[2]),
                }
        meminfo: dict[str, int | None] = {}
        for line in lines[2:]:
            match = re.match(r"^(\w+):\s+(\d+)", line or "")
            if match:
                meminfo[match.group(1)] = to_int(match.group(2))
        if meminfo.get("MemTotal"):
            total = meminfo.get("MemTotal") or 0
            available = meminfo.get("MemAvailable") or 0
            used = max(total - available, 0)
            payload["memory"] = {
                "total_kb": total,
                "available_kb": available,
                "used_kb": used,
                "percent": round(used * 100.0 / total, 1) if total else None,
            }
        return payload

    @staticmethod
    def _parse_df(raw: str) -> list[dict[str, Any]]:
        filesystems: list[dict[str, Any]] = []
        seen_mounts: set[str] = set()
        for line in raw.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6 or parts[0].startswith("none") or not parts[1].isdigit():
                continue
            total_kb, used_kb, avail_kb, use_pct, mount = (
                int(parts[1]), int(parts[2]), int(parts[3]), parts[4], parts[5],
            )
            if mount in {"/dev", "/proc", "/sys", "/run", "/boot/efi"} or mount.startswith("/snap"):
                continue
            if mount in seen_mounts:
                continue
            seen_mounts.add(mount)
            filesystems.append({
                "filesystem": parts[0][:64],
                "mount": mount[:80],
                "total_kb": total_kb,
                "used_kb": used_kb,
                "available_kb": avail_kb,
                "percent": to_float(use_pct.rstrip("%")),
            })
        return filesystems[:12]
