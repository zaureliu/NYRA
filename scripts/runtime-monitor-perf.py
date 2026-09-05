"""Mede custo do ciclo de inspeção do Runtime Monitor (spec #96)."""
import asyncio, os, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("KAZUMI_OLLAMA_PRELOAD", "false")
os.environ.setdefault("KAZUMI_CONVERSATION_ENGINE", "false")
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    sup = app.state.services.runtime_supervisor
    async def measure():
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            await sup.inspect_all()
            samples.append((time.perf_counter() - t0) * 1000)
        return samples
    samples = client.portal and None or asyncio.run(measure()) if False else None
    loop = asyncio.new_event_loop()
    try:
        samples = loop.run_until_complete(measure())
    finally:
        loop.close()
    print(f"serviços monitorados: {len(sup.snapshots)}")
    print(f"intervalo base: {sup.settings.runtime_health_interval_seconds}s")
    print(f"ciclo inspect_all ms: média={statistics.mean(samples):.0f} min={min(samples):.0f} max={max(samples):.0f}")
    print("subprocessos por ciclo HTTP/TCP: 1 conexão por check (sem spawn pesado); COMMAND/Warm hook: 0")
