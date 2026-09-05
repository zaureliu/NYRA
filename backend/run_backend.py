"""KAZUMI backend standalone entrypoint (empacotado com PyInstaller).

Modo dev: ``python -m uvicorn app.main:app`` continua o fluxo oficial.
Modo instalado: este módulo é o entrypoint do ``kazumi-backend.exe`` —
bootstrap do layout %LOCALAPPDATA%\\KAZUMI, servidor em 127.0.0.1:8000 e
código de saída que sinaliza restart intencional ao launcher Tauri:

    * exit 0  -> encerramento normal/shutdown (Tauri NÃO relança);
    * exit 75 -> restart completo pedido pelo operador (Tauri relança).

Watchdog (§8): no modo empacotado não há watchdog (não dá para empacotar
sem Python/PowerShell externos) — desabilitado com honestidade via env.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--sapi-worker":
        # PyInstaller executables do not implement ``python -m``. Dispatch the
        # isolated Windows SAPI worker inside the packaged backend executable.
        from app.speech.sapi_worker import main as sapi_main

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return sapi_main()
    if getattr(sys, "frozen", False):
        os.environ.setdefault("KAZUMI_FROZEN", "1")
        # Watchdog não existe no pacote final: sem Python global/.venv/PowerShell.
        os.environ.setdefault("KAZUMI_WATCHDOG_ENABLED", "0")
    elif os.environ.get("KAZUMI_FROZEN") == "1":
        # Permite validar o layout instalado rodando pelo venv (smoke de packaging).
        os.environ.setdefault("KAZUMI_WATCHDOG_ENABLED", "0")

    from app.core.paths import ensure_runtime_directories

    ensure_runtime_directories()

    import uvicorn  # noqa: PLC0415 - import tardio após bootstrap de paths

    from app.main import app  # noqa: PLC0415 - settings dependem dos paths finais

    port = int(os.environ.get("KAZUMI_BACKEND_PORT", "8000"))
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )

    # Servidor parou: distingue shutdown de restart intencional para o Tauri.
    try:
        from app.core.lifecycle import peek_intentional_flag

        flag = peek_intentional_flag()
        if isinstance(flag, dict) and str(flag.get("kind")) == "restart":
            return 75
    except Exception:  # noqa: BLE001 - saída nunca deve falhar aqui
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
