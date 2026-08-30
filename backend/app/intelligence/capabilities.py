from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.intelligence.models import RuntimeState


Probe = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]


class CapabilityRegistryV2:
    """Runtime authority. A declared capability is not available until its probe says so."""

    STATE_MAP = {
        "READY": RuntimeState.AVAILABLE, "ONLINE": RuntimeState.AVAILABLE,
        "AVAILABLE": RuntimeState.AVAILABLE, "DEGRADED": RuntimeState.DEGRADED,
        "FAILED": RuntimeState.OFFLINE, "OFFLINE": RuntimeState.OFFLINE,
        "DISABLED": RuntimeState.DISABLED, "UNCONFIGURED": RuntimeState.UNCONFIGURED,
        "BLOCKED": RuntimeState.BLOCKED,
    }

    def __init__(self, legacy_provider: Probe | None = None) -> None:
        self.legacy_provider = legacy_provider
        self._probes: dict[str, tuple[str, Probe, tuple[str, ...]]] = {}
        self._last: dict[str, Any] = {"capabilities": [], "summary": {}}
        self._lock = asyncio.Lock()

    def register(self, capability_id: str, description: str, probe: Probe,
                 *, dependencies: tuple[str, ...] = ()) -> None:
        self._probes[capability_id] = (description, probe, dependencies)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            entries: dict[str, dict[str, Any]] = {}
            legacy = await self._call(self.legacy_provider)
            for raw in legacy.get("capabilities", []) if isinstance(legacy, dict) else []:
                capability_id = str(raw.get("id") or "")
                if not capability_id:
                    continue
                state = self.STATE_MAP.get(str(raw.get("runtime_state") or raw.get("health") or "UNKNOWN").upper(), RuntimeState.UNKNOWN)
                entries[capability_id] = {
                    "id": capability_id, "name": raw.get("name") or capability_id,
                    "description": raw.get("description") or "", "state": state.value,
                    "configured": bool(raw.get("configured", True)), "dependencies": [],
                    "health": raw.get("health"), "last_error": raw.get("last_error"),
                    "source": "runtime_capabilities_v1",
                }
            for capability_id, (description, probe, dependencies) in self._probes.items():
                result = await self._call(probe)
                raw_state = str(result.get("state") or "UNKNOWN").upper()
                state = self.STATE_MAP.get(raw_state, RuntimeState.UNKNOWN)
                missing = [dependency for dependency in dependencies if entries.get(dependency, {}).get("state") not in {RuntimeState.AVAILABLE.value, RuntimeState.DEGRADED.value}]
                if missing and state == RuntimeState.AVAILABLE:
                    state = RuntimeState.BLOCKED
                entries[capability_id] = {
                    "id": capability_id, "name": result.get("name") or capability_id,
                    "description": description, "state": state.value,
                    "configured": state != RuntimeState.UNCONFIGURED,
                    "dependencies": list(dependencies), "missing_dependencies": missing,
                    "health": result.get("health") or raw_state, "last_error": result.get("error_code"),
                    "source": "intelligence_platform", "details": result.get("details", {}),
                }
            values = sorted(entries.values(), key=lambda item: item["id"])
            summary = {state.value: sum(item["state"] == state.value for item in values) for state in RuntimeState}
            self._last = {"capabilities": values, "summary": summary, "observed_at": datetime.now(timezone.utc).isoformat()}
            return self._last

    async def available(self, capability_id: str) -> bool:
        snapshot = await self.snapshot()
        item = next((value for value in snapshot["capabilities"] if value["id"] == capability_id), None)
        return bool(item and item["state"] in {RuntimeState.AVAILABLE.value, RuntimeState.DEGRADED.value})

    async def natural_summary(self) -> dict[str, Any]:
        snapshot = await self.snapshot()
        available = [item["name"] for item in snapshot["capabilities"] if item["state"] == RuntimeState.AVAILABLE.value]
        degraded = [item["name"] for item in snapshot["capabilities"] if item["state"] == RuntimeState.DEGRADED.value]
        unavailable = [item["name"] for item in snapshot["capabilities"] if item["state"] not in {RuntimeState.AVAILABLE.value, RuntimeState.DEGRADED.value}]
        return {"available": available, "degraded": degraded, "unavailable": unavailable, "source": "live_capability_registry"}

    @staticmethod
    async def _call(probe: Probe | None) -> dict[str, Any]:
        if probe is None:
            return {}
        try:
            value = probe()
            if asyncio.iscoroutine(value):
                value = await value
            return value if isinstance(value, dict) else {}
        except Exception as error:
            return {"state": "OFFLINE", "error_code": type(error).__name__}
