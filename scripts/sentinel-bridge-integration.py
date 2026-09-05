"""Real local integration check. Requires an explicitly enabled test Sentinel bridge."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx
import psutil
import aiosqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.events import EventBus, EventType
from app.integrations.sentinel.auth import SentinelSecretStore
from app.integrations.sentinel.connector import SentinelConnector
from app.integrations.sentinel.models import SentinelState


async def run(url: str, timeout: float) -> int:
    token = str(os.environ.get("KAZUMI_SENTINEL_TEST_TOKEN", "") or "").strip()
    if len(token) < 32:
        print("KAZUMI_SENTINEL_TEST_TOKEN must contain at least 32 characters", file=sys.stderr)
        return 2
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        print("Invalid --url", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="kazumi-sentinel-integration-", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        settings = Settings.from_sources(
            environment="test",
            database_path=root / "kazumi.db",
            sentinel_watch_enabled=True,
            sentinel_auto_discovery=False,
            sentinel_host=parsed.hostname,
            sentinel_port=parsed.port or 5000,
            sentinel_voice_alerts=False,
            sentinel_store_event_history=True,
            sentinel_debug_mode=True,
            sentinel_discovery_interval=15,
        )
        bus = EventBus()
        connector = SentinelConnector(settings, bus)
        connector.secrets = SentinelSecretStore(root / "secrets" / "token.txt")
        connector.secrets.save(token)
        event_received = asyncio.Event()
        event_at = 0.0

        async def subscriber(event):
            nonlocal event_at
            if event.type == EventType.SENTINEL_EVENT:
                event_at = time.perf_counter()
                event_received.set()

        await bus.subscribe(subscriber)
        started = time.perf_counter()
        await connector.initialize()
        try:
            deadline = time.monotonic() + timeout
            while connector.state != SentinelState.CONNECTED and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            connected_ms = (time.perf_counter() - started) * 1000
            if connector.state != SentinelState.CONNECTED:
                print({"connected": False, "state": connector.state.value, "error": connector.status()["last_error"]})
                return 1
            event_received.clear()  # replay is measured separately from the live event
            event_at = 0.0
            sent_at = time.perf_counter()
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    f"{url.rstrip('/')}/api/integrations/kazumi/debug/events/warning",
                    headers={"Authorization": f"Bearer {token}"},
                )
            response.raise_for_status()
            await asyncio.wait_for(event_received.wait(), timeout=timeout)
            event_ms = (event_at - sent_at) * 1000
            history = await connector.history.recent(hours=1, limit=10)
            async with aiosqlite.connect(settings.database_path) as database:
                total_history_rows = (await (await database.execute("SELECT COUNT(*) FROM sentinel_events")).fetchone())[0]
            process = psutil.Process()
            process.cpu_percent(None)
            await asyncio.sleep(2)
            idle_cpu = process.cpu_percent(None)
            rss_mb = round(process.memory_info().rss / 1024 / 1024, 2)
            reconnect_started = time.perf_counter()
            await connector.reconnect()
            reconnect_deadline = time.monotonic() + timeout
            while connector.state != SentinelState.CONNECTED and time.monotonic() < reconnect_deadline:
                await asyncio.sleep(0.05)
            reconnect_ms = (time.perf_counter() - reconnect_started) * 1000
            reconnect_ok = connector.state == SentinelState.CONNECTED
            await connector.stop()
            disabled_ok = connector.state == SentinelState.DISABLED and connector._client is None
            print({
                "connected": True,
                "state": SentinelState.CONNECTED.value,
                "connect_ms": round(connected_ms, 2),
                "event_to_kazumi_ms": round(event_ms, 2),
                "events_received": connector.status()["events_received"],
                "history_rows": len(history),
                "history_total_rows": total_history_rows,
                "event_type": history[0]["type"] if history else None,
                "connected_idle_cpu_percent": idle_cpu,
                "process_rss_mb": rss_mb,
                "reconnect_ok": reconnect_ok,
                "reconnect_ms": round(reconnect_ms, 2),
                "off_closes_transport": disabled_ok,
            })
            return 0
        finally:
            await connector.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5501")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    return asyncio.run(run(args.url, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
