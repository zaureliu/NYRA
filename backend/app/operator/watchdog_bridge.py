"""Watchdog bridge (§226-§228): lets the LIVE backend ask the external
watchdog for a restart via one-shot request files. When the backend is dead,
the watchdog's own health checks handle recovery without any help.

Subscribed to RUNTIME_FAILED / RUNTIME_CRASH_LOOP events about nyra_backend
and writes data/watchdog-requests/<id>.json with cooldown protection.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.core.paths import DATA_ROOT

_REQUESTS_DIR = DATA_ROOT / "watchdog-requests"
_MIN_INTERVAL_SECONDS = 120.0


class WatchdogBridge:
    def __init__(self, requests_dir: Path | None = None, *,
                 min_interval_seconds: float = _MIN_INTERVAL_SECONDS) -> None:
        self.requests_dir = Path(requests_dir or _REQUESTS_DIR)
        self.min_interval_seconds = min_interval_seconds
        self._last_request: float = 0.0
        self.metrics = {"requests_written": 0, "suppressed": 0}

    async def handle_event(self, event) -> None:
        event_type = str(getattr(event, "type", ""))
        if event_type not in {"RUNTIME_FAILED", "RUNTIME_CRASH_LOOP"}:
            return
        payload = getattr(event, "payload", {}) or {}
        service_id = str(payload.get("service_id") or "")
        if service_id and service_id != "nyra_backend":
            return
        await self.request_backend_restart(
            reason=str(payload.get("reason") or payload.get("error_code") or event_type)[:120]
        )

    async def request_backend_restart(self, *, reason: str = "") -> bool:
        now = time.time()
        if now - self._last_request < self.min_interval_seconds:
            self.metrics["suppressed"] += 1
            return False
        try:
            self.requests_dir.mkdir(parents=True, exist_ok=True)
            request_path = self.requests_dir / f"restart-{os.urandom(4).hex()}.json"
            request_path.write_text(json.dumps({
                "action": "restart_backend",
                "reason": reason[:200],
                "requested_at": now,
                "source": "runtime_supervisor_bridge",
            }), encoding="utf-8")
        except OSError:
            return False
        self._last_request = now
        self.metrics["requests_written"] += 1
        return True
