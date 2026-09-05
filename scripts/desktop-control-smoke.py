"""Smoke REAL do Desktop Application Control V1.

Aceite prioritário: "Kazumi, abre o bloco de notas" deve terminar com a janela
efetivamente VISÍVEL no desktop e VERIFICADA por enumeração Win32 real,
com checagem cruzada independente (tasklist). Encerramos apenas o PID que a
KAZUMI lançou — instâncias pré-existentes do operador não são tocadas.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("KAZUMI_OLLAMA_PRELOAD", "false")
os.environ.setdefault("KAZUMI_CONVERSATION_ENGINE", "false")

import json

from fastapi.testclient import TestClient

from app.main import app


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=1)[:1200])


def notepad_pids_external() -> set[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq notepad.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout
    pids = set()
    for line in out.splitlines():
        parts = [part.strip() for part in line.split('","')]
        if len(parts) >= 2 and parts[0].strip('"').casefold() == "notepad.exe":
            try:
                pids.add(int(parts[1].strip('"')))
            except ValueError:
                continue
    return pids


def main() -> None:
    before = notepad_pids_external()
    print("notepad pré-existente (operador):", before or "nenhum")

    with TestClient(app) as client:
        apps = client.get("/api/desktop/apps")
        assert apps.status_code == 200
        listing = apps.json()
        ids = {item["id"] for item in listing["apps"]}
        print("\ntools/registry:", sorted(ids))
        assert {"notepad", "calculadora", "paint", "explorer"} <= ids

        unknown = client.post("/api/desktop/apps/inexistente/launch")
        payload_unknown = unknown.json()
        assert payload_unknown["error_code"] == "UNKNOWN_APP"
        show("APP INEXISTENTE bloqueado", payload_unknown)

        launched = client.post("/api/desktop/apps/notepad/launch")
        assert launched.status_code == 200
        result = launched.json()
        show("LAUNCH notepad", result)
        assert result["success"] is True, result
        assert result["execution_success"] is True
        assert result["effect_verified"] is True
        assert result["verification_status"] == "VERIFIED"
        assert result["windows"], "sem janela confirmada"
        window_pid = int(result["windows"][0]["pid"])
        assert window_pid > 0 and window_pid not in before, "pid da janela deve ser NOVO"

        current = client.get("/api/desktop/windows?app=notepad").json()
        show("WINDOWS atuais", current)
        assert current["open"] is True

    after_launch = notepad_pids_external()
    show("CHECAGEM INDEPENDENTE (tasklist)", {"pids": sorted(after_launch), "novo_pid_presente": window_pid in after_launch})
    assert window_pid in after_launch, "janela não está visível no desktop segundo o SO"

    subprocess.run(["taskkill", "/PID", str(window_pid), "/T", "/F"],
                   capture_output=True, text=True)
    import time
    time.sleep(1.0)
    final = notepad_pids_external()
    show("APÓS FECHAR NOSSO PID", {"restantes": sorted(final), "nosso_pid_vivo": window_pid in final})
    assert window_pid not in final

    print("\nSMOKE DESKTOP CONTROL: OK — janela efetivamente visível e verificada.")


if __name__ == "__main__":
    main()
