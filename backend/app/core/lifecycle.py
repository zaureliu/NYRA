"""Coordenação central de encerramento/restart completo (closure Partes 10-13).

Regras estruturais:

* Watchdog é desarmado ANTES do backend sair (§11.1) via canal one-shot
  ``data/watchdog-requests/*.json`` — o mesmo canal do Runtime Supervisor.
* Flag intencional persistida distingue encerramento pedido pelo operador de
  crash real; startup reconciliation consome o arquivo (§15).
* Nenhum processo externo/shared é morto: apenas sinalizamos a saída do
  próprio backend; componentes owned são parados pelo lifespan com steps
  limitados (_graceful_shutdown em app.main).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT, LOG_ROOT, PROJECT_ROOT

logger = logging.getLogger("kazumi.lifecycle")

WATCHDOG_REQUESTS_DIR = DATA_ROOT / "watchdog-requests"
INTENTIONAL_SHUTDOWN_FLAG = DATA_ROOT / "runtime-intentional-shutdown.json"
RESTART_LAUNCHER = PROJECT_ROOT / "scripts" / "restart-session.ps1"

_shutting_down = False


def is_shutting_down() -> bool:
    return _shutting_down


def _write_json(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as error:
        logger.warning("lifecycle_write_failed path=%s error=%s", path.name, type(error).__name__)
        return False


def write_intentional_flag(kind: str, reason: str) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "reason": reason[:200],
        "requested_at": time.time(),
        "session": uuid.uuid4().hex[:12],
    }
    _write_json(INTENTIONAL_SHUTDOWN_FLAG, payload)
    return payload


def consume_intentional_flag() -> dict[str, Any] | None:
    """Startup reconciliation (§15): lê e remove a flag de shutdown intencional."""
    try:
        if not INTENTIONAL_SHUTDOWN_FLAG.is_file():
            return None
        document = json.loads(INTENTIONAL_SHUTDOWN_FLAG.read_text(encoding="utf-8"))
        INTENTIONAL_SHUTDOWN_FLAG.unlink(missing_ok=True)
        return document if isinstance(document, dict) else None
    except (OSError, ValueError):
        return None


def peek_intentional_flag() -> dict[str, Any] | None:
    """Leitura SEM consumo (packaging): o launcher Tauri consulta após a saída
    do processo para distinguir restart intencional (relança) de crash."""
    try:
        if not INTENTIONAL_SHUTDOWN_FLAG.is_file():
            return None
        document = json.loads(INTENTIONAL_SHUTDOWN_FLAG.read_text(encoding="utf-8"))
        return document if isinstance(document, dict) else None
    except (OSError, ValueError):
        return None


def _is_frozen() -> bool:
    import sys

    return bool(getattr(sys, "frozen", False)) or os.environ.get("KAZUMI_FROZEN") == "1"


def disarm_watchdog(reason: str) -> bool:
    """Canal one-shot: watchdog consome, NÃO relança backend e se encerra."""
    request_id = f"shutdown-{uuid.uuid4().hex[:10]}.json"
    return _write_json(WATCHDOG_REQUESTS_DIR / request_id, {
        "action": "shutdown",
        "reason": reason[:160],
        "requested_at": time.time(),
    })


def trigger_server_exit() -> None:
    """SIGINT no próprio processo: uvicorn faz shutdown graceful do lifespan.

    Executado em thread separada porque raise_signal dentro do event loop
    pode ser reentrante com o handler do uvicorn.
    """

    def _raise() -> None:
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except (OSError, ValueError):
            os._exit(0)

    threading.Thread(target=_raise, name="kazumi-power-exit", daemon=True).start()


def parent_disappeared_shutdown() -> None:
    """Fail-safe used only by an owned packaged backend."""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    write_intentional_flag("shutdown", "desktop_parent_disappeared")
    disarm_watchdog("desktop_parent_disappeared")
    logger.warning("shutdown_requested reason=desktop_parent_disappeared")
    trigger_server_exit()


async def coordinate_full_shutdown(reason: str = "operator_request") -> dict[str, Any]:
    """Fluxo §11: flag → disarm watchdog → rejeitar novo trabalho → exit.

    Os componentes owned (voz, browser, presence, brokers, jobs, DB) são
    parados pelo lifespan em ordem única; este coordenador garante que o
    watchdog não relance nada durante a janela de saída.
    """
    global _shutting_down
    if _shutting_down:
        return {"state": "SHUTDOWN_ALREADY_REQUESTED"}
    _shutting_down = True
    flag = write_intentional_flag("shutdown", reason)
    disarmed = disarm_watchdog(reason)
    logger.info("shutdown_requested reason=%s watchdog_disarmed=%s", reason[:80], disarmed)
    trigger_server_exit()
    return {"state": "SHUTDOWN_REQUESTED", "flag": flag["session"], "watchdog_disarmed": disarmed}


def spawn_restart_launcher(timeout_seconds: int = 120) -> bool:
    """Agenda a nova sessão DEPOIS da saída desta (§13): porta livre → launcher.

    O script detached espera a porta 8000 liberar antes de chamar o launcher
    oficial (start-kazumi.ps1), que gera nova runtime_session_id.
    """
    import subprocess

    if not RESTART_LAUNCHER.is_file():
        # Fallback direto: PowerShell embutido espera porta livre e chama start-kazumi.ps1.
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            f"$deadline=(Get-Date).AddSeconds({timeout_seconds});"
            "while((Get-Date) -lt $deadline){"
            "if(-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)){break}"
            "Start-Sleep -Milliseconds 800};"
            f"& '{PROJECT_ROOT / 'scripts' / 'start-kazumi.ps1'}'",
        ]
    else:
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(RESTART_LAUNCHER),
            "-TimeoutSeconds", str(timeout_seconds),
        ]
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log_dir = LOG_ROOT
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "restart-session.log", "ab")  # noqa: SIM115
        subprocess.Popen(  # noqa: S603,S606
            command, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
            stdout=handle, stderr=handle, close_fds=True, creationflags=creationflags,
        )
        handle.close()
        return True
    except OSError as error:
        logger.error("restart_spawn_failed error=%s", type(error).__name__)
        return False


async def coordinate_full_restart(reason: str = "operator_request") -> dict[str, Any]:
    global _shutting_down
    if _shutting_down:
        return {"state": "SHUTDOWN_ALREADY_REQUESTED"}
    _shutting_down = True
    flag = write_intentional_flag("restart", reason)
    disarmed = disarm_watchdog(reason)
    # Packaging (§7/§8): no modo instalado NÃO há PowerShell/scripts — o launcher
    # Tauri observa a saída do processo (exit code 75 de run_backend.py) e abre
    # a nova sessão do backend sozinho. Em dev, o fluxo legado é preservado.
    spawned = False if _is_frozen() else spawn_restart_launcher()
    logger.info(
        "full_restart_requested",
        extra={"reason": reason[:80], "launcher_spawned": spawned,
               "frozen": _is_frozen(), "watchdog_disarmed": disarmed},
    )
    trigger_server_exit()
    return {
        "state": "RESTART_REQUESTED",
        "flag": flag["session"],
        "launcher_spawned": spawned,
        "watchdog_disarmed": disarmed,
    }
