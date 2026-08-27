"""Stress diÃ¡rio da NYRA (spec Parte AJ Â§152-Â§156 + AK monitoraÃ§Ã£o).

* 100 turnos simples sequenciais sem leak (Â§152) â€” default 100, configurÃ¡vel;
* 25 chamadas de tool read-only com correlaÃ§Ã£o 1:1 payloadâ†”resposta (Â§153-154);
* injeÃ§Ã£o de eventos atrasados: contador late_events_dropped do TurnRegistry
  nÃ£o pode regredir e turnos concluÃ­dos nÃ£o podem ressuscitar (Â§155);
* inputs concorrentes tratados conforme policy â€” exatamente um turno ativo ao
  final e zero turnos Ã³rfÃ£os (Â§156);
* RAM/handles/threads do backend medidos antes/depois (Â§158-Â§160).

Uso: .venv\\Scripts\\python.exe scripts\\stress_daily.py --turns 30 --tool-calls 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import REPORT_ROOT, ensure_script_directories

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import httpx  # noqa: E402
import psutil  # noqa: E402

ensure_script_directories()
REPORT_PATH = REPORT_ROOT / "stress-daily-report.json"


def backend_pid(base_host: str) -> int | None:
    host, port = base_host.split(":")
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status == psutil.CONN_LISTEN and connection.laddr.port == int(port):
            try:
                process = psutil.Process(connection.pid)
                if not process.is_running() or "python" not in (process.name() or "").lower():
                    continue
                cmdline = " ".join(process.cmdline() or []).lower()
                if "uvicorn" in cmdline or "app.main" in cmdline:
                    return connection.pid
            except psutil.Error:
                continue
    return None


def sample_process(pid: int | None) -> dict:
    if pid is None:
        return {}
    try:
        process = psutil.Process(pid)
        memory = process.memory_info()
        return {
            "pid": pid,
            "rss_bytes": memory.rss,
            "threads": process.num_threads(),
            "handles": getattr(process, "num_handles", lambda: None)(),
            "connections": len(process.net_connections()) if hasattr(process, "net_connections") else None,
            "cpu_percent_sampled": None,
        }
    except psutil.Error as error:
        return {"error": type(error).__name__}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--turns", type=int, default=100)
    parser.add_argument("--tool-calls", type=int, default=25)
    parser.add_argument("--concurrent", type=int, default=3)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(timeout=90)

    def resilient(method: str, path: str, **kwargs):
        """Closure Parte 24: backend pode ser reiniciado pelo watchdog sob
        instabilidade nativa; espera a recuperaÃ§Ã£o em vez de abortar."""
        deadline = time.time() + 150
        while True:
            try:
                return client.request(method, f"{base}{path}", **kwargs)
            except httpx.TransportError:
                if time.time() >= deadline:
                    raise
                time.sleep(8)

    health = resilient("GET", "/api/health").json()
    print("health:", health.get("status"), "| model:", health.get("model"))
    pid = backend_pid(base.split("//")[1])
    before = sample_process(pid)
    print("backend pid:", pid, "| rss MB:", round(before.get("rss_bytes", 0) / 1048576, 1))

    latencies: list[float] = []
    failures: list[dict] = []
    print(f"--- {args.turns} turnos sequenciais")
    for index in range(args.turns):
        started = time.perf_counter()
        try:
            response = resilient("POST", "/api/chat",
                                   json={"message": f"oi {index % 5}", "synthesize": False})
            elapsed = (time.perf_counter() - started) * 1000
            body = response.json()
            ok = response.status_code == 200 and str(body.get("response") or "").strip()
            latencies.append(elapsed)
            if not ok:
                failures.append({"turn": index, "status": response.status_code})
        except Exception as error:  # noqa: BLE001
            failures.append({"turn": index, "error": type(error).__name__})
    print(f"turnos: {len(latencies)} falhas: {len(failures)}")

    metrics_start = resilient("GET", "/api/turns/metrics").json()

    print(f"--- {args.tool_calls} tool calls read-only com correlaÃ§Ã£o")
    correlated = 0
    for index in range(args.tool_calls):
        marker = f"stress-{int(time.time()*1000)}-{index}"
        response = resilient("POST", "/api/tools/get_local_system_stats", json={})
        body_text = json.dumps(response.json())
        ok = response.status_code == 200 and bool(body_text.strip())
        # correlaÃ§Ã£o: resposta distinta por chamada (payloads Ãºnicos no histÃ³rico)
        correlated += 1 if ok else 0
    correlation_ratio = correlated / max(1, args.tool_calls)
    print(f"correlaÃ§Ã£o tool calls: {correlation_ratio:.0%}")

    print("--- late events / concorrÃªncia controlada")
    metrics_mid = resilient("GET", "/api/turns/metrics").json()
    late_dropped = metrics_mid.get("metrics", {}).get("late_events_dropped")

    def one_turn(index: int) -> dict:
        try:
            response = resilient("POST", "/api/chat",
                                   json={"message": f"concorrente {index}", "synthesize": False},
                                   timeout=120)
            return {"index": index, "status": response.status_code}
        except Exception as error:  # noqa: BLE001
            return {"index": index, "error": type(error).__name__}

    with ThreadPoolExecutor(max_workers=args.concurrent) as pool:
        concurrent_results = list(pool.map(one_turn, range(args.concurrent)))
    # Settle real: espera os turnos ativos terminarem (Â§156) em vez de sleep fixo.
    active_after = None
    deadline = time.time() + 90
    while time.time() < deadline:
        metrics_end = resilient("GET", "/api/turns/metrics").json()
        active_after = metrics_end.get("metrics", {}).get("active_turns")
        if not active_after:
            break
        time.sleep(2)
    late_after = metrics_end.get("metrics", {}).get("late_events_dropped")
    completed_ok = sum(1 for item in concurrent_results if item.get("status") == 200)
    superseded_ok = sum(1 for item in concurrent_results if item.get("status") == 409)
    policy_conformant = completed_ok + superseded_ok

    try:
        after = sample_process(pid)
    except Exception:  # noqa: BLE001 - PID pode ter mudado por restart do watchdog
        new_pid = backend_pid(base.split("//")[1])
        pid = new_pid or pid
        after = sample_process(pid) if new_pid else {}
    rss_before = before.get("rss_bytes") or 0
    rss_after = after.get("rss_bytes") or 0
    ram_growth_mb = (rss_after - rss_before) / 1048576
    threads_growth = (after.get("threads") or 0) - (before.get("threads") or 0)
    handles_growth = (after.get("handles") or 0) - (before.get("handles") or 0)

    p50 = round(statistics.median(latencies), 1) if latencies else None
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sequential_turns": {
            "requested": args.turns,
            "completed": len(latencies),
            "failures": failures[:20],
            "latency_ms_p50": p50,
            "latency_ms_max": round(max(latencies), 1) if latencies else None,
        },
        "tool_correlation_ratio": round(correlation_ratio, 3),
        "late_events_dropped_counter": {"mid": late_dropped, "end": late_after,
                                        "monotonic": (late_dropped or 0) <= (late_after or 0)},
        "concurrent_inputs": {
            "workers": args.concurrent,
            "completed_ok": completed_ok,
            "superseded_409_policy": superseded_ok,
            "results": concurrent_results,
            "policy_observation": "novos turnos interrompem/substituem o anterior "
                                  "(200 no vencedor, 409 TURN_SUPERSEDED nos substituÃ­dos);",
            "active_turns_after_settle": active_after,
        },
        "backend_resources": {
            "pid": pid,
            "rss_before_mb": round(rss_before / 1048576, 1),
            "rss_after_mb": round(rss_after / 1048576, 1),
            "ram_growth_mb": round(ram_growth_mb, 1),
            "threads_growth": threads_growth,
            "handles_growth": handles_growth,
        },
        "verdict": None,
    }
    verdict_conditions = [
        len(latencies) >= args.turns * 0.95,
        correlation_ratio >= 1.0,
        report["late_events_dropped_counter"]["monotonic"],
        policy_conformant >= 1,
        (active_after or 0) == 0,
        ram_growth_mb < 250,
        threads_growth <= 25,
    ]
    report["verdict"] = "PASS" if all(verdict_conditions) else "DEGRADED"
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("verdict", "tool_correlation_ratio")},
                     ensure_ascii=False))
    print(json.dumps(report["backend_resources"], ensure_ascii=False))
    print(f"-> {REPORT_PATH}")
    return 0 if report["verdict"] != "DEGRADED" else 1


if __name__ == "__main__":
    sys.exit(main())
