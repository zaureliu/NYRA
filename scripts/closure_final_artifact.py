"""Closure Parte 51 — consolida o artefato final .tmp/release-health-final.json.

Somente leitura de evidências já produzidas nesta closure; NUNCA fabrica
resultado: cada campo vem de um arquivo de prova ou é marcado como pendente.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from runtime_paths import DATA_ROOT, REPORT_ROOT, ensure_script_directories

REPO = Path(__file__).resolve().parents[1]
ensure_script_directories()
TMP = REPORT_ROOT
OUT = TMP / "release-health-final.json"

HARD_GATES = {
    "backend": ("backend pytest completo", "0 fail"),
    "frontend_tests": ("frontend vitest", "exit 0"),
    "vite_build": ("frontend build", "exit 0"),
    "daily_use": (".tmp/daily-use-report.json overall != FAIL", "PASS/DEGRADED"),
    "stress": (".tmp/stress-daily-report.json verdict", "PASS"),
    "notepad_e2e": (".tmp/notepad-final-result.json verdict", "PASS"),
    "long_run": (".tmp/long-run-report.json leak_suspected", False),
}


def git_head() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO),
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.strip()[:12] if out.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> int:
    gate = read_json(TMP / "release-health.json") or {}
    daily = read_json(TMP / "daily-use-report.json") or {}
    stress = read_json(TMP / "stress-daily-report.json") or {}
    notepad = read_json(TMP / "notepad-final-result.json") or {}
    longrun = read_json(TMP / "long-run-report.json") or {}
    watchdog_hb = read_json(DATA_ROOT / "watchdog-heartbeat.json") or {}
    # Evidência direta da closure vence o gate com co-load (§0.5: medição
    # sequencial é a válida; gate paralelo degradava o próprio runtime).
    backend_direct = read_json(TMP / "backend-full-final.json")

    steps = {step.get("step"): step.get("exit_code")
             for step in gate.get("steps", [])}

    if backend_direct is not None:
        backend_exit = int(backend_direct.get("exit_code", 1))
        backend_summary = str(backend_direct.get("summary", ""))
    else:
        backend_exit = next((code for name, code in steps.items()
                             if str(name).startswith("backend pytest")), None)
        backend_summary = (gate.get("summary") or {}).get("backend")
    frontend_test_exit = steps.get("frontend vitest")
    vite_exit = next((code for name, code in steps.items()
                      if "build" in str(name)), None)

    results = {
        "generated_at": time.time(),
        "git_head": git_head(),
        "version": "0.2.0",
        "gate_verdict": gate.get("verdict"),
        "gate_generated_at": gate.get("generated_at"),
        "results": {
            "backend_full": {"exit_code": backend_exit, "summary": backend_summary},
            "frontend_vitest": {"exit_code": frontend_test_exit},
            "vite_tsc_build": {"exit_code": vite_exit},
            "daily_use": {"overall": daily.get("overall"), "counts": daily.get("counts")},
            "stress": {"verdict": stress.get("verdict"),
                       "tool_correlation_ratio": stress.get("tool_correlation_ratio")},
            "notepad_e2e": {"verdict": notepad.get("verdict"),
                            "file_verify": notepad.get("file_verify"),
                            "window_absent": notepad.get("window_absent")},
            "long_run": {"minutes": longrun.get("minutes"),
                         "samples": longrun.get("samples"),
                         "leak_suspected": longrun.get("leak_suspected"),
                         "growth_percent": longrun.get("growth_percent")},
            "watchdog": {"heartbeat_age_seconds": round(max(0.0, time.time() - float(watchdog_hb.get("timestamp", 0))), 1)
                         if watchdog_hb.get("timestamp") else None},
        },
    }

    failures = []
    if backend_exit not in (0,):
        failures.append("backend_full")
    if frontend_test_exit != 0:
        failures.append("frontend_vitest")
    if vite_exit != 0:
        failures.append("vite_tsc_build")
    if daily.get("overall") == "FAIL":
        failures.append("daily_use")
    if stress.get("verdict") != "PASS":
        failures.append("stress")
    if notepad.get("verdict") != "PASS":
        failures.append("notepad_e2e")
    if longrun.get("leak_suspected") is not False:
        failures.append("long_run")

    results["hard_failures"] = failures
    results["final_verdict"] = "RED" if failures else "GREEN"
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"final_verdict": results["final_verdict"],
                      "hard_failures": failures}, ensure_ascii=False))
    print(f"-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
