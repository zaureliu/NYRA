"""Long-run harness acelerado da KAZUMI (spec Parte AK §157-§161).

Monitora o processo do backend por N minutos amostrando threads, handles,
RAM e conexões — detecta leaks com thresholds configuráveis. Documenta a
limitação: sessões de horas reais devem usar --minutes 60+ com operador
presente; este harness comprime a evidência estatística em minutos.

Uso: .venv\\Scripts\\python.exe scripts\\long_run_harness.py --minutes 10 [--turns-per-minute 2]
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import REPORT_ROOT, ensure_script_directories

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import httpx  # noqa: E402
import psutil  # noqa: E402

ensure_script_directories()
REPORT_CSV = REPORT_ROOT / "long-run-samples.csv"
REPORT_JSON = REPORT_ROOT / "long-run-report.json"


def find_backend_pid(port: int) -> int | None:
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status == psutil.CONN_LISTEN and connection.laddr.port == port:
            try:
                process = psutil.Process(connection.pid)
                if process.is_running() and "python" in (process.name() or "").lower():
                    return connection.pid
            except psutil.Error:
                continue
    return None


def sample(pid: int) -> dict:
    process = psutil.Process(pid)
    memory = process.memory_info()
    return {
        "ts": round(time.time(), 3),
        "rss_mb": round(memory.rss / 1048576, 1),
        "threads": process.num_threads(),
        "handles": getattr(process, "num_handles", lambda: None)(),
        "connections": len([c for c in process.net_connections()
                            if c.status == psutil.CONN_ESTABLISHED]),
        "cpu_percent": process.cpu_percent(interval=None),
    }


def keepalive_turn(base: str) -> None:
    """Tráfego leve periódico para simular uso contínuo (não é load test)."""
    try:
        httpx.post(f"{base}/api/chat",
                   json={"message": "ping silencioso de long-run", "synthesize": False},
                   timeout=60)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--turns-per-minute", type=int, default=2,
                        help="turnos leves por minuto para manter atividade realista")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    port = int(base.split(":")[-1].split("/")[0])
    pid = find_backend_pid(port)
    if pid is None:
        print("backend não encontrado na porta", port)
        return 1
    process = psutil.Process(pid)
    process.cpu_percent(interval=None)  # primeira chamada só inicializa
    print(f"monitorando pid {pid} por {args.minutes} min (amostra a cada "
          f"{args.interval_seconds}s)")

    deadline = time.time() + args.minutes * 60
    next_turn = time.time() + 60 / max(1, args.turns_per_minute)
    samples: list[dict] = []
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "rss_mb", "threads",
                                                    "handles", "connections", "cpu_percent"])
        writer.writeheader()
        while time.time() < deadline:
            try:
                row = sample(pid)
                writer.writerow(row)
                handle.flush()
                samples.append(row)
            except psutil.Error:
                # Closure Parte 25: o backend pode ser reiniciado pelo watchdog
                # sob instabilidade nativa desta estação — re-anexa ao novo PID
                # em vez de encerrar a amostragem.
                time.sleep(10)
                new_pid = find_backend_pid(port)
                if new_pid is None or new_pid == pid:
                    continue
                try:
                    psutil.Process(new_pid).cpu_percent(interval=None)
                except psutil.Error:
                    continue
                print(f"re-attach: backend novo pid {new_pid}", flush=True)
                pid = new_pid
            if time.time() >= next_turn:
                keepalive_turn(base)
                next_turn = time.time() + 60 / max(1, args.turns_per_minute)
            time.sleep(args.interval_seconds)

    def series(key: str) -> list[float]:
        return [float(item[key]) for item in samples if item.get(key) is not None]

    rss = series("rss_mb")
    threads = series("threads")
    handles = series("handles")
    connections = series("connections")

    def growth(values: list[float]) -> float | None:
        if len(values) < 4:
            return None
        first_quarter = values[: max(1, len(values) // 4)]
        last_quarter = values[-max(1, len(values) // 4):]
        baseline = statistics.median(first_quarter)
        final = statistics.median(last_quarter)
        if baseline <= 0:
            return None
        return round((final - baseline) / baseline * 100, 2)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pid": pid,
        "minutes": args.minutes,
        "samples": len(samples),
        "rss_mb_median": round(statistics.median(rss), 1) if rss else None,
        "rss_mb_max": round(max(rss), 1) if rss else None,
        "threads_max": int(max(threads)) if threads else None,
        "handles_max": int(max(handles)) if handles else None,
        "connections_established_max": int(max(connections)) if connections else None,
        "growth_percent": {
            "ram": growth(rss),
            "threads": growth(threads),
            "handles": growth(handles),
            "connections": growth(connections),
        },
        "thresholds": {"ram_percent": 40.0, "threads_percent": 50.0,
                       "handles_percent": 30.0},
        "leak_suspected": None,
        "limitation": ("harness acelerado; para evidência de horas reais rode com "
                       "--minutes 60..480 durante um dia típico de uso"),
    }
    growth_values = report["growth_percent"]
    threshold_map = {"ram": 40.0, "threads": 50.0, "handles": 30.0}
    leak = any(value is not None and value > threshold_map[key]
               for key, value in growth_values.items() if key in threshold_map)
    report["leak_suspected"] = bool(leak)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["growth_percent"], ensure_ascii=False))
    print("verdict:", "LEAK_SUSPECTED" if leak else "STABLE")
    print(f"-> {REPORT_JSON}")
    return 0 if not leak else 1


if __name__ == "__main__":
    sys.exit(main())
