"""Closure Parte 18 — Notepad E2E REAL contra o runtime da NYRA (:8000).

Fluxo obrigatório (§18.1):
    launch → verify PID/HWND/title → type → read back →
    save em .test-temp/notepad-final-validation.txt → verify file →
    close graceful (WM_CLOSE) → verify window absent → cleanup.

Sem taskkill como caminho normal (§18.3).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
TARGET_TEXT = "NYRA final validation"
SAVE_PATH = REPO_ROOT / ".test-temp" / "notepad-final-validation.txt"

results: dict[str, object] = {"case": "notepad_final_validation"}


def tool(client: httpx.Client, name: str, payload: dict) -> dict:
    response = client.post(f"{BASE}/api/tools/{name}", json={"parameters": payload}, timeout=60)
    document = response.json()
    data = document.get("data") if isinstance(document.get("data"), dict) else document
    results[f"tool_{name}"] = {
        "http": response.status_code,
        "success": data.get("success"),
        "error_code": data.get("error_code"),
        "effect_verified": data.get("effect_verified"),
        "verification_status": data.get("verification_status"),
    }
    if response.status_code != 200 or data.get("success") is not True:
        raise AssertionError(f"{name} falhou: {json.dumps(document, ensure_ascii=False)[:400]}")
    return data


def notepad_pids() -> set[int]:
    out = subprocess.run(  # noqa: S603
        ["tasklist", "/FI", "IMAGENAME eq notepad.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and parts[0].strip('"').casefold() == "notepad.exe":
            try:
                pids.add(int(parts[1].strip('"')))
            except ValueError:
                continue
    return pids


def main() -> int:
    SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    before = notepad_pids()
    results["pre_existing_pids"] = sorted(before)

    with httpx.Client(timeout=90) as client:
        # 1. launch com verificação real de janela visível
        launched = tool(client, "desktop_launch", {"app": "notepad"})
        windows = launched.get("windows") or []
        assert windows, "nenhuma janela confirmada após launch"
        window = windows[0]
        pid, hwnd, title = int(window["pid"]), window.get("hwnd"), str(window.get("title") or "")
        results["pid"] = pid
        results["hwnd"] = hwnd
        results["visible_title"] = title
        assert pid > 0 and pid not in before, "PID deve ser novo e visível"
        assert launched.get("effect_verified") is True and launched.get("verification_status") == "VERIFIED"

        # checagem cruzada independente (SO): janela realmente no desktop
        assert pid in notepad_pids(), "PID não aparece no tasklist do SO"

        time.sleep(0.6)

        def send_keys(payload: dict, attempts: int = 3) -> dict:
            # §18.2: enumeração de janela pode falhar transitoriamente — retry curto.
            last = {}
            for attempt in range(attempts):
                document = tool(client, "ui_send_keys", payload)
                if document.get("success") is True or document.get("error_code") != "WINDOW_NOT_FOUND":
                    return document
                last = document
                time.sleep(1.0 + attempt)
            return last

        # Estratégia de save robusta (Win11 Notepad usa diálogo WinUI, não
        # #32770): cria o arquivo-alvo vazio, abre no Notepad (path associado),
        # digita o texto e o Ctrl+S salva SEM diálogo.
        SAVE_PATH.write_text("", encoding="utf-8")
        opened = tool(client, "desktop_close", {"hwnd": hwnd})
        assert opened.get("success") is True, "fecho da janela sem título falhou"
        deadline = time.time() + 6
        while time.time() < deadline and pid in notepad_pids():
            time.sleep(0.3)
        tool(client, "desktop_open_file", {"path": str(SAVE_PATH), "app": "notepad"})
        # abrir não retorna janela: consultar windows até o arquivo aparecer com PID novo
        window = None
        deadline = time.time() + 12
        while time.time() < deadline and window is None:
            status = tool(client, "desktop_windows", {"app": "notepad"})
            for item in status.get("windows", []):
                if SAVE_PATH.stem.casefold() in str(item.get("title") or "").casefold():
                    window = item
                    break
            if window is None:
                time.sleep(0.5)
        assert window is not None, "janela do arquivo aberto não apareceu"
        pid, hwnd = int(window["pid"]), window.get("hwnd")
        results["opened_path_pid"] = pid
        results["visible_title"] = str(window.get("title") or "")[:80]
        time.sleep(0.8)

        # digitar texto com input REAL de teclado (marca o buffer como modificado;
        # UIA SetValue não suja o arquivo e o Ctrl+S viraria no-op).
        # Documento vazio recém-aberto já tem o caret no Edit; ui_click tem um
        # bug COM conhecido (TypeError) e não é necessário aqui.
        typed = send_keys({"text": TARGET_TEXT, "hwnd": hwnd})
        results["typed"] = typed.get("success")

        # read back exato
        readback = tool(client, "ui_get_text", {"control_type": "Edit", "app": "notepad"})
        value = str(readback.get("value") or readback.get("text") or "")
        results["readback"] = value[:80]
        assert TARGET_TEXT in value, f"texto lido difere: {value!r}"

        focused = tool(client, "desktop_focus", {"app": "notepad"})
        assert focused.get("success") is True
        # sintaxe do engine SendInput: {ctrl+s} (a sintaxe ^s NÃO é suportada)
        send_keys({"text": "{ctrl+s}", "hwnd": hwnd})
        deadline = time.time() + 8
        saved_content = ""
        while time.time() < deadline:
            saved_content = SAVE_PATH.read_text(encoding="utf-8").strip() if SAVE_PATH.is_file() else ""
            if saved_content == TARGET_TEXT:
                break
            time.sleep(0.5)
        results["saved_content"] = saved_content[:80]
        assert saved_content == TARGET_TEXT, f"conteúdo salvo difere: {saved_content!r}"
        results["file_verify"] = True

        # fechar gracioso (WM_CLOSE) e verificar ausência
        closed = tool(client, "desktop_close", {"app": "notepad"})
        results["closed_gracefully"] = bool(closed.get("windows_absent", closed.get("success")))
        deadline = time.time() + 8
        while time.time() < deadline and pid in notepad_pids():
            time.sleep(0.4)
        assert pid not in notepad_pids(), "janela/PID ainda presente após WM_CLOSE"
        results["window_absent"] = True

    # 6. cleanup do arquivo de evidência temporário
    SAVE_PATH.unlink(missing_ok=True)
    results["cleanup"] = True
    results["verdict"] = "PASS"
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as failure:
        results["verdict"] = "FAIL"
        results["failure"] = str(failure)[:300]
        print(json.dumps(results, ensure_ascii=False, indent=1))
        raise SystemExit(1)
