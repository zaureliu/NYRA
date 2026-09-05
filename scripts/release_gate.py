"""Regression Gate / Release Health da KAZUMI (spec Parte BE §248-§250, BF).

Executa a bateria local e classifica o build:

    GREEN  — backend, frontend, build, diff-check e E2E core OK
    YELLOW — ok com ressalvas (integrações opcionais SKIPPED/DEGRADED,
             sem baseline de benchmark ainda)
    RED    — qualquer falha de gate obrigatório

Gates obrigatórios: backend tests, frontend tests, vite build, turn isolation
(incluída no backend), grounding (incluído no backend), daily core E2E.
Integrações opcionais podem ficar SKIPPED/UNCONFIGURED (§249).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import DATA_ROOT, REPORT_ROOT, ensure_script_directories

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
FRONTEND_DIR = REPO_ROOT / "frontend"
ensure_script_directories()
REPORT_PATH = REPORT_ROOT / "release-health.json"
PROGRESS_PATH = REPORT_ROOT / "release-gate-progress.json"
GATE_STARTED_AT = time.time()


def publish_progress(step_index: int, total_steps: int, current_step: str) -> None:
    """Progresso N/M para a UI (closure §21): RUNNING + etapa atual."""
    try:
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_PATH.write_text(json.dumps({
            "started_at": GATE_STARTED_AT,
            "step_index": step_index,
            "total_steps": total_steps,
            "current_step": current_step,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_step(name: str, command: list[str], *, cwd: Path | None = None,
             timeout: float = 1800) -> dict:
    print(f"\n=== {name}\n$ {' '.join(command)}", flush=True)
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=str(cwd or REPO_ROOT), capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout)
    elapsed = round(time.perf_counter() - started, 1)
    output_tail = "\n".join((completed.stdout or "").splitlines()[-6:])
    print(output_tail or f"(exit {completed.returncode})")
    return {
        "step": name,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "duration_s": elapsed,
        "stdout_tail": output_tail[-1500:],
        "stderr_tail": (completed.stderr or "")[-800:],
    }


def parse_pytest_count(tail: str) -> str | None:
    for line in tail.splitlines():
        if " passed" in line:
            return line.strip()[-120:]
    return None


def current_git_head() -> str:
    try:
        completed = subprocess.run(["git", "rev-parse", "--short", "HEAD"],  # noqa: S603
                                   cwd=str(REPO_ROOT), capture_output=True, text=True,
                                   timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode == 0:
            return completed.stdout.strip()[:12]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def main() -> int:
    # Console Windows pode ser cp1252: saída de ferramentas (tsc/vite/pytest)
    # contém box-drawing/acentos e derrubaria o gate com UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-daily-e2e", action="store_true",
                        help="pula o daily-use E2E (não recomendado para release)")
    parser.add_argument("--skip-stress", action="store_true")
    args = parser.parse_args()

    steps: list[dict] = []
    summary_extra: dict = {}

    total_steps = 3 + (0 if args.skip_daily_e2e else 1) + (0 if args.skip_stress else 1)
    publish_progress(1, total_steps, "git diff --check")
    steps.append(run_step("git diff --check",
                          ["git", "diff", "--check"]))
    publish_progress(2, total_steps, "backend pytest completo")
    steps.append(run_step("backend pytest completo",
                          [str(VENV_PY), "-m", "pytest", "backend/tests", "-q",
                           "--tb=line", "-p", "no:cacheprovider"], timeout=2400))
    if steps[-1]["exit_code"] < 0:
        # Windows nativo (access violation em libs COM/WinRT/whisper) pode matar
        # o processo pytest de forma não-determinística; um retry limpo separa
        # crash ambiental de regressão real.
        print("pytest sofreu crash nativo (exit negativo); executando retry limpo", flush=True)
        steps.append(run_step("backend pytest completo (retry pós crash nativo)",
                              [str(VENV_PY), "-m", "pytest", "backend/tests", "-q",
                               "--tb=line", "-p", "no:cacheprovider"], timeout=2400))
    backend_step = next(step for step in steps
                        if step["step"].startswith("backend pytest"))
    pytest_summary = parse_pytest_count(backend_step["stdout_tail"])
    summary_extra["backend"] = pytest_summary

    publish_progress(3, total_steps, "frontend vitest")
    steps.append(run_step("frontend vitest",
                          ["npm.cmd", "test"], cwd=FRONTEND_DIR, timeout=900))
    publish_progress(4, total_steps, "frontend build (tsc + vite)")
    steps.append(run_step("frontend build (tsc + vite)",
                          ["npm.cmd", "run", "build"], cwd=FRONTEND_DIR, timeout=900))

    if not args.skip_daily_e2e:
        publish_progress(5, total_steps, "daily-use E2E (runtime real)")
        steps.append(run_step("daily-use E2E (runtime real)",
                              [str(VENV_PY), "scripts/daily_use_e2e.py"], timeout=3600))
        report_file = REPORT_ROOT / "daily-use-report.json"
        if report_file.exists():
            document = json.loads(report_file.read_text(encoding="utf-8"))
            summary_extra["daily_use_overall"] = document.get("overall")
            summary_extra["daily_use_counts"] = document.get("counts")

    if not args.skip_stress:
        publish_progress(6, total_steps, "stress diário acelerado")
        steps.append(run_step("stress diário acelerado",
                              [str(VENV_PY), "scripts/stress_daily.py",
                               "--turns", "30", "--tool-calls", "10"],
                              timeout=3600))
        stress_file = REPORT_ROOT / "stress-daily-report.json"
        if stress_file.exists():
            summary_extra["stress_verdict"] = json.loads(
                stress_file.read_text(encoding="utf-8")).get("verdict")

    # ------------------------------------------------------------- avaliação
    hard_failures = []
    warnings = []

    git_check = next(step for step in steps if step["step"] == "git diff --check")
    if git_check["exit_code"] != 0:
        hard_failures.append("git diff --check com ruído de whitespace")

    backend_step = next(step for step in steps
                        if step["step"].startswith("backend pytest"))
    if backend_step["exit_code"] != 0:
        hard_failures.append("backend tests falhando")

    frontend_test = next(step for step in steps if step["step"] == "frontend vitest")
    if frontend_test["exit_code"] != 0:
        hard_failures.append("frontend tests falhando")

    frontend_build = next(step for step in steps if "build" in step["step"])
    if frontend_build["exit_code"] != 0:
        hard_failures.append("vite/tsc build falhou")

    daily_overall = summary_extra.get("daily_use_overall")
    if not args.skip_daily_e2e:
        if daily_overall == "FAIL":
            hard_failures.append("daily-use E2E com FAIL")
        elif daily_overall == "DEGRADED":
            warnings.append("daily-use E2E DEGRADED (ver cenários)")

    stress_verdict = summary_extra.get("stress_verdict")
    if stress_verdict == "DEGRADED":
        warnings.append("stress com ressava (ver .tmp/stress-daily-report.json)")

    benchmarks_dir = DATA_ROOT / "model-benchmarks" / "baselines"
    has_baseline = benchmarks_dir.exists() and any(benchmarks_dir.glob("*.json"))
    if not has_baseline:
        warnings.append("baseline oficial do qwen3:8b ainda não salva "
                        "(rode POST /api/benchmark/full + baselines/save)")

    if hard_failures:
        verdict = "RED"
    elif warnings:
        verdict = "YELLOW"
    else:
        verdict = "GREEN"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": current_git_head(),
        "verdict": verdict,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "summary": summary_extra,
        "steps": [{"step": item["step"], "exit_code": item["exit_code"],
                   "duration_s": item["duration_s"]} for item in steps],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n================ RELEASE HEALTH ================")
    print("verdict:", verdict)
    for failure in hard_failures:
        print("  HARD FAIL:", failure)
    for warning in warnings:
        print("  warn     :", warning)
    print(f"-> {REPORT_PATH}")
    return 0 if verdict != "RED" else 1


if __name__ == "__main__":
    sys.exit(main())
