"""OpenWrt structured reads using ubus/ifstatus/logread (all classified READ_ONLY)."""

from __future__ import annotations

import re
from typing import Any

from app.homelab.adapters.base import SshAdapterError, SshHostAdapter, to_float, to_int


class OpenWrtAdapter(SshHostAdapter):
    platform_label = "openwrt"

    async def status(self) -> dict[str, Any]:
        info_raw = await self.run("ubus call system info", reason="homelab:openwrt:status")
        release_raw = await self.run("cat /etc/openwrt_release", reason="homelab:openwrt:status")
        dump_raw = await self.run("ubus call network.interface dump", timeout_seconds=12, reason="homelab:openwrt:status")
        info = self.parse_json_output(info_raw) or {}
        payload: dict[str, Any] = {
            "uptime_s": to_float(info.get("uptime")),
            "load": _load_array(info.get("load")),
            "memory": {
                "total": to_int((info.get("memory") or {}).get("total")),
                "free": to_int((info.get("memory") or {}).get("free")),
            },
            "release": self._parse_release(release_raw),
        }
        dump = self.parse_json_output(dump_raw)
        entries = dump.get("interface") if isinstance(dump, dict) else None
        if isinstance(entries, list):
            payload["wan"] = _summarize_wan(entries)
            payload["lan"] = _summarize_lan(entries)
            payload["default_route"] = _default_route(entries)
        return payload

    async def interfaces(self) -> dict[str, Any]:
        dump_raw = await self.run(
            "ubus call network.interface dump", timeout_seconds=12, reason="homelab:openwrt:interfaces",
        )
        dump = self.parse_json_output(dump_raw)
        entries = dump.get("interface") if isinstance(dump, dict) else None
        if not isinstance(entries, list):
            raise SshAdapterError("OPENWRT_PARSE_FAILED", "Não foi possível interpretar o dump de interfaces.")
        return {"interfaces": [_interface_entry(item) for item in entries]}

    async def wifi_status(self) -> dict[str, Any]:
        raw = await self.run(
            "ubus call network.wireless status", timeout_seconds=8, reason="homelab:openwrt:wifi",
        )
        data = self.parse_json_output(raw)
        if not isinstance(data, dict):
            raise SshAdapterError("OPENWRT_PARSE_FAILED", "Sem resposta wireless do ubus.")
        radios: list[dict[str, Any]] = []
        for radio_name, radio in data.items():
            if not isinstance(radio, dict):
                continue
            radios.append({
                "radio": str(radio_name)[:32],
                "up": bool(radio.get("up")),
                "interfaces": [
                    {
                        "ifname": str(iface.get("ifname") or "")[:32],
                        "mode": str((iface.get("config") or {}).get("mode") or "")[:24],
                    }
                    for iface in (radio.get("interfaces") or [])[:6]
                    if isinstance(iface, dict)
                ],
            })
        return {"radios": radios}

    async def logs(self, lines: int = 30) -> dict[str, Any]:
        bounded = max(1, min(int(lines), 120))
        raw = await self.run(f"logread | tail -n {bounded}", timeout_seconds=10, reason="homelab:openwrt:logs")
        return {"lines": [line[:400] for line in raw.splitlines()[-bounded:]]}

    @staticmethod
    def _parse_release(raw: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for match in re.finditer(r"^(DISTRIB_[A-Z_]+)='([^']*)'", raw, re.MULTILINE):
            fields[match.group(1)] = match.group(2)[:80]
        return fields


def _summarize_wan(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        routes = entry.get("route") or []
        has_default = any(isinstance(r, dict) and str(r.get("target")) == "0.0.0.0/0" for r in routes)
        proto_is_wan = str(entry.get("proto") or "").lower().startswith(("dhcp", "pppoe", "static"))
        if bool(entry.get("up")) and has_default and proto_is_wan:
            return _interface_entry(entry)
    return None


def _summarize_lan(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        name = str(entry.get("interface") or "")
        if isinstance(entry, dict) and entry.get("up") and name.lower().startswith("lan"):
            return _interface_entry(entry)
    return None


def _interface_entry(entry: dict[str, Any]) -> dict[str, Any]:
    addresses = [
        str(addr.get("address") or "")[:64]
        for addr in (entry.get("ipv4-address") or [])
        if isinstance(addr, dict)
    ]
    return {
        "interface": str(entry.get("interface") or "")[:40],
        "up": bool(entry.get("up")),
        "proto": str(entry.get("proto") or "")[:24],
        "addresses": addresses[:8],
        "device": str(entry.get("device") or "")[:32],
    }


def _default_route(entries: list[dict[str, Any]]) -> dict[str, str] | None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for route in entry.get("route") or []:
            if isinstance(route, dict) and str(route.get("target")) == "0.0.0.0/0":
                return {
                    "gateway": str(route.get("nexthop") or "")[:64],
                    "interface": str(entry.get("interface") or "")[:40],
                }
    return None


def _load_array(value: Any) -> list[float | None]:
    if isinstance(value, list):
        result: list[float | None] = []
        for item in value[:3]:
            try:
                result.append(round(float(item) / 65536.0, 2))
            except (TypeError, ValueError):
                result.append(None)
        return result
    return []
