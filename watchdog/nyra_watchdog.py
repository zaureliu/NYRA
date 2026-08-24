#!/usr/bin/env python3
"""NYRA External Watchdog (spec Parte L §212-§231).

Independent from the main backend: pure stdlib, NO LLM/Ollama dependency
(§215), deliberately simple (§216). Survives backend death and brings it back.

Health checks (§218):
    backend  HTTP GET /api/health
    frontend TCP host:port
    ollama   HTTP GET /api/tags
    desktop  process name presence (Desktop Presence)

Recovery policy (§219/§226): after N consecutive failures, restart the
component (backend restart is built-in; other components are opt-in via
NYRA_WATCHDOG_<NAME>_CMD), then verify health again.

Restart limit (§220/§221): max RESTART_LIMIT restarts per RESTART_WINDOW
seconds per component -> CRASH_LOOP_PROTECTED (no infinite retries).

Extras:
    - heartbeat JSON written every cycle for GET /api/watchdog/status;
    - consumes one-shot request files in data/watchdog-requests/
      ({action:"restart_backend", reason}) so the Runtime Supervisor can ask
      for an external restart while still alive (§227).

No admin required (§224). No self-update in V1 (§225).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "logs"
DATA_DIR = REPO_ROOT / "data"
HEARTBEAT_PATH = DATA_DIR / "watchdog-heartbeat.json"
REQUESTS_DIR = DATA_DIR / "watchdog-requests"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"

BACKEND_URL = os.environ.get("NYRA_WATCHDOG_BACKEND_URL", "http://127.0.0.1:8000/api/health")
FRONTEND_HOST = os.environ.get("NYRA_WATCHDOG_FRONTEND_HOST", "127.0.0.1")
FRONTEND_PORT = int(os.environ.get("NYRA_WATCHDOG_FRONTEND_PORT", "5173"))
OLLAMA_URL = os.environ.get("NYRA_WATCHDOG_OLLAMA_URL", "http://127.0.0.1:11434/api/tags")
DESKTOP_PROCESS = os.environ.get("NYRA_WATCHDOG_DESKTOP_PROCESS", "nyra-desktop.exe")
INTERVAL = float(os.environ.get("NYRA_WATCHDOG_INTERVAL", "10"))
FAILURE_THRESHOLD = int(os.environ.get("NYRA_WATCHDOG_FAILURE_THRESHOLD", "3"))
RESTART_LIMIT = int(os.environ.get("NYRA_WATCHDOG_RESTART_LIMIT", "3"))
RESTART_WINDOW = float(os.environ.get("NYRA_WATCHDOG_RESTART_WINDOW", "600"))
FRONTEND_RESTART_CMD = os.environ.get("NYRA_WATCHDOG_FRONTEND_CMD", "")
DESKTOP_RESTART_CMD = os.environ.get("NYRA_WATCHDOG_DESKTOP_CMD", "")


def log(message: str) -> None:
    """Separate watchdog log (§222)."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}"
    try:
        print(line, flush=True)
    except OSError:
        pass
    try:
        WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except Exception:  # noqa: BLE001 - any failure counts as unhealthy
        return False


def tcp_ok(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def process_running(name: str) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603
            ["tasklist.exe", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = completed.stdout or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return name.casefold() in raw.casefold()
    except (OSError, subprocess.TimeoutExpired):
        return False


class ComponentGuard:
    """Failure counter + crash-loop protection (§219-§221)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.consecutive_failures = 0
        self.restarts: list[float] = []

    def record(self, healthy: bool) -> str | None:
        if healthy:
            self.consecutive_failures = 0
            return None
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAILURE_THRESHOLD:
            now = time.time()
            self.restarts = [stamp for stamp in self.restarts if now - stamp <= RESTART_WINDOW]
            if len(self.restarts) >= RESTART_LIMIT:
                log(f"{self.name}: CRASH_LOOP_PROTECTED ({len(self.restarts)} restarts em {RESTART_WINDOW:.0f}s)")
                self.consecutive_failures = 0
                return "CRASH_LOOP_PROTECTED"
            return "RESTART"
        return None

    def mark_restart(self) -> None:
        self.restarts.append(time.time())
        self.consecutive_failures = 0


def _detached(command: list[str], cwd: Path | None = None, log_name: str = "") -> bool:
    try:
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        out_target = LOG_DIR / (log_name or "watchdog-spawn.log")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handle = open(out_target, "ab")  # noqa: SIM115
        subprocess.Popen(  # noqa: S603,S606 - comando configurado pelo operador local
            command, cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
            close_fds=True, creationflags=creationflags,
        )
        return True
    except OSError as exc:
        log(f"spawn_failed: {exc}")
        return False


def restart_backend() -> bool:
    # Closure Parte 9.2 (runtime resolver único): NUNCA cair para o Python
    # global como fallback silencioso — sem .venv oficial, não relança.
    python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        log("backend: relançamento ABORTADO — runtime oficial (.venv) ausente")
        return False
    ok = _detached(
        [str(python), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", os.environ.get("NYRA_WATCHDOG_BACKEND_PORT", "8000")],
        cwd=REPO_ROOT / "backend",
        log_name="watchdog-backend-restart.log",
    )
    if ok:
        log("backend: restart solicitado (uvicorn desanexado)")
    return ok


def run_once() -> dict:
    states = {
        "backend": http_ok(BACKEND_URL),
        "frontend": FRONTEND_PORT == 0 or tcp_ok(FRONTEND_HOST, FRONTEND_PORT),
        "ollama": http_ok(OLLAMA_URL),
        "desktop": process_running(DESKTOP_PROCESS),
    }
    return states


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    guards = {name: ComponentGuard(name) for name in ("backend", "frontend", "ollama", "desktop")}
    log(f"watchdog started (interval={INTERVAL}s, threshold={FAILURE_THRESHOLD}, "
        f"restart_limit={RESTART_LIMIT}/{RESTART_WINDOW:.0f}s)")
    while True:
        # Requests primeiro (closure §11.1/§16.1): um shutdown intencional
        # escrito pelo backend precisa ser honrado ANTES de qualquer decisão
        # de restart baseada em health — senão o watchdog relançaria o
        # backend durante o encerramento.
        if _consume_requests(guards):
            return 0
        states = run_once()
        decisions: dict[str, str] = {}
        for name, healthy in states.items():
            decision = guards[name].record(healthy)
            decisions[name] = decision or ""
            if decision == "RESTART":
                guards[name].mark_restart()
                if name == "backend":
                    restart_backend()
                elif name == "frontend" and FRONTEND_RESTART_CMD:
                    _detached(FRONTEND_RESTART_CMD.split(), cwd=REPO_ROOT / "frontend",
                              log_name="watchdog-frontend-restart.log")
                elif name == "desktop" and DESKTOP_RESTART_CMD:
                    _detached(DESKTOP_RESTART_CMD.split(),
                              log_name="watchdog-desktop-restart.log")
                else:
                    log(f"{name}: unhealthy mas sem comando de restart configurado (monitor-only)")

        heartbeat = {
            "watchdog_pid": os.getpid(),
            "timestamp": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "components": states,
            "decisions": decisions,
            "restart_counts": {name: len(item.restarts) for name, item in guards.items()},
        }
        try:
            tmp = HEARTBEAT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(heartbeat), encoding="utf-8")
            os.replace(tmp, HEARTBEAT_PATH)
        except OSError:
            pass
        time.sleep(INTERVAL)


def _consume_requests(guards: dict[str, ComponentGuard]) -> bool:
    """One-shot external requests (Runtime Supervisor channel, §227).

    Retorna True quando o watchdog deve SE ENCERRAR (shutdown intencional
    pedido pelo Shutdown Coordinator) — sem relançar nenhum componente."""
    try:
        files = sorted(REQUESTS_DIR.glob("*.json"))
    except OSError:
        return False
    shutdown_requested = False
    for path in files[:5]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        action = str(document.get("action") or "")
        reason = str(document.get("reason") or "")[:120]
        path.unlink(missing_ok=True)
        if action == "shutdown":
            log(f"intentional_shutdown recebido razao={reason}; watchdog encerrando sem relançar")
            try:
                heartbeat = {
                    "watchdog_pid": os.getpid(),
                    "timestamp": time.time(),
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "intentional_shutdown": True,
                    "reason": reason,
                }
                tmp = HEARTBEAT_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(heartbeat), encoding="utf-8")
                os.replace(tmp, HEARTBEAT_PATH)
            except OSError:
                pass
            shutdown_requested = True
            continue
        if action == "restart_backend":
            guard = guards["backend"]
            now = time.time()
            guard.restarts = [stamp for stamp in guard.restarts if now - stamp <= RESTART_WINDOW]
            if len(guard.restarts) >= RESTART_LIMIT:
                log(f"request restart_backend NEGADO (crash loop) razao={reason}")
                continue
            guard.mark_restart()
            log(f"request restart_backend aceito razao={reason}")
            restart_backend()
    return shutdown_requested


if __name__ == "__main__":
    raise SystemExit(main())
