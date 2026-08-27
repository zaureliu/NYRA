"""Manual real-network integration probe; not part of the offline unit suite."""

import asyncio
import argparse
import json
from pathlib import Path
import sys
import time

import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.events import EventBus
from app.network_watch.monitor import NetworkWatchMonitor


async def main(duration: int) -> None:
    settings = Settings.from_sources(network_watch_enabled=False)
    monitor = NetworkWatchMonitor(settings, EventBus())
    await monitor.initialize()
    process = psutil.Process()
    cpu_started = process.cpu_times()
    started = time.perf_counter()
    monitor.enabled = True
    result = await monitor.poll_once(force=True)
    while time.perf_counter() - started < duration:
        await asyncio.sleep(.5)
        result = await monitor.poll_once(force=False)
    cpu_finished = process.cpu_times()
    wall = max(.001, time.perf_counter() - started)
    cpu_percent = ((cpu_finished.user + cpu_finished.system) - (cpu_started.user + cpu_started.system)) / wall * 100
    print(json.dumps({"status": result["status"], "snapshot": result["snapshot"], "samples": len(monitor.samples), "process_cpu_percent": round(cpu_percent, 2), "process_ram_mb": round(process.memory_info().rss / 1024**2, 2)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=1)
    arguments = parser.parse_args()
    asyncio.run(main(max(1, arguments.duration)))
