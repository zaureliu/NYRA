"""E2E REAL do Operador Universal no runtime da janela KAZUMI (kazumi-full §22-§30).

Requisitos:
  * backend REAL em 127.0.0.1:8000 (mesmo comando do launcher);
  * janela REAL kazumi-desktop.exe aberta (reusa o backend da porta 8000);
  * comandos enviados pela MESMA rota usada pela UI (POST /api/chat);
  * verificação de efeito INDEPENDENTE (enumeração Win32 neste processo,
    fora do backend); effect_verified só quando o SO confirma;
  * evidência de rota por turno via logs do backend (fast path vs Agent Loop).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from runtime_paths import REPORT_ROOT, ensure_script_directories

REPO = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.paths import DATA_ROOT  # noqa: E402
from app.desktop.windows import annotate_process_names, list_visible_windows  # noqa: E402

BASE = "http://127.0.0.1:8000"
ensure_script_directories()
RESULTS_PATH = REPORT_ROOT / "e2e-universal-runtime-results.jsonl"


def http_json(path: str, payload: dict | None = None, timeout: float = 90.0):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def windows_now() -> list[dict]:
    return [
        {"hwnd": w.hwnd, "pid": w.pid, "title": w.title,
         "process": (w.process_name or "").casefold()}
        for w in annotate_process_names(list_visible_windows())
    ]


def proc_stem(name: str) -> str:
    return name.removesuffix(".exe").casefold()


def count_windows(process_hint: str, title_contains: str = "") -> list[dict]:
    hint = proc_stem(process_hint)
    token = title_contains.casefold()
    return [
        w for w in windows_now()
        if hint and hint in proc_stem(w["process"])
        and (not token or token in w["title"].casefold())
    ]


def wait_for(predicate, timeout: float, interval: float = 0.4) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def backend_log_grep(turn_marker: str) -> str:
    """Extrai linhas dos logs do backend para o turno (evidência de rota)."""
    lines_out: list[str] = []
    for name in ("e2e-backend.stderr.log", "e2e-backend.stdout.log"):
        try:
            content = (REPO / "logs" / name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines_out.extend(line for line in content.splitlines() if turn_marker in line)
    return "\n".join(lines_out[-14:])


def run_command(message: str, expect_fast: bool | None = None, settle: float = 1.0) -> dict:
    started = time.perf_counter()
    entry: dict = {"input": message}
    response = None
    for attempt in range(3):
        try:
            response = http_json("/api/chat", {"message": message, "synthesize": False})
            break
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            entry["error"] = f"{type(exc).__name__}: {text}"[:200]
            # 409 TURN_SUPERSEDED: input de voz da janela real venceu o turno.
            # Política real do runtime; repete o comando do usuário.
            if "409" in text and attempt < 2:
                time.sleep(2.5)
                continue
            response = None
            break
    if response is not None:
        entry.pop("error", None)
        entry["reply"] = response.get("response", "")
        entry["turn_id"] = response.get("turn_id")
        entry["status"] = response.get("pipeline_status")
    else:
        entry.setdefault("reply", "")
        entry.setdefault("turn_id", "")
    entry["latency_s"] = round(time.perf_counter() - started, 2)
    time.sleep(settle)
    marker = str(entry.get("turn_id") or "")
    evidence = backend_log_grep(marker)
    entry["route_evidence"] = {
        "desktop_op": [line for line in evidence.splitlines() if "desktop_operation" in line],
        "agent_loop": [line for line in evidence.splitlines()
                       if "agent" in line.casefold() and "run" in line.casefold()],
        "raw_tail": evidence.splitlines()[-4:],
    }
    if expect_fast is not None:
        hit_desktop_op = bool(entry["route_evidence"]["desktop_op"])
        entry["expect_fast_ok"] = (hit_desktop_op and not entry["route_evidence"]["agent_loop"]) if expect_fast else True
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{entry['latency_s']:>5}s] {message} -> {entry['reply'][:110]}", flush=True)
    return entry


def main() -> int:
    health = http_json("/health")
    print("HEALTH:", json.dumps({k: health.get(k) for k in ("status", "llm", "model")}, ensure_ascii=False))
    runtime = {
        "backend_port": 8000,
        "health": {k: health.get(k) for k in ("status", "character", "llm")},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # Silencia o canal de voz da JANELA REAL durante a bateria determinística
    # (o mic aberto injeta turnos concorrentes — política §156 de supersede).
    try:
        request = urllib.request.Request(
            BASE + "/api/listening/settings", method="PUT",
            data=json.dumps({"enabled": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as resp:
            resp.read()
        runtime["listening_disabled_for_battery"] = True
    except Exception as exc:  # noqa: BLE001
        runtime["listening_disabled_for_battery"] = f"fail: {exc}"
    desktop_pids = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process kazumi-desktop -ErrorAction SilentlyContinue | "
         "Select-Object -First 1 Id,Path | ConvertTo-Json"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    runtime["desktop"] = desktop_pids
    backend_pid = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | "
         "Select-Object -First 1).OwningProcess"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    runtime["backend_pid"] = backend_pid
    if backend_pid.isdigit():
        runtime["backend_path"] = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {backend_pid}).Path"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()

    pre_existing = {w["hwnd"] for w in windows_now()}
    results: dict[str, dict] = {}

    # ---------------------------------------------------------- §27 dedup
    before_np = count_windows("notepad")
    r1 = run_command("abre o bloco de notas", expect_fast=True)
    after_np = wait_for(lambda: len(count_windows("notepad")) >= len(before_np) + 1, 12)
    r1["effect_verified"] = after_np
    r1["windows_delta"] = len(count_windows("notepad")) - len(before_np)
    results["open_notepad_dedup_1"] = r1

    r2 = run_command("abre o bloco de notas", expect_fast=True)
    r2["windows_delta"] = len(count_windows("notepad")) - len(before_np)
    r2["no_duplicate"] = len(count_windows("notepad")) <= len(before_np) + 1
    results["open_notepad_again_no_dup"] = r2

    # ---------------------------------------------------------- §24 window chain (Code)
    results["open_code"] = run_command("abre o Code", expect_fast=True)
    wait_for(lambda: bool(count_windows("Code")), 15)
    code_before = count_windows("Code")
    results["open_code"]["effect_verified"] = bool(code_before)

    results["minimiza_ele"] = run_command("minimiza ele", expect_fast=True)
    time.sleep(0.8)

    def code_iconic() -> bool:
        from app.desktop.window_manager import window_state
        return any(window_state(w["hwnd"])["iconic"] for w in count_windows("Code"))

    results["minimiza_ele"]["effect_verified"] = wait_for(code_iconic, 5)

    results["restaura_ele"] = run_command("restaura ele", expect_fast=True)

    def code_not_iconic() -> bool:
        from app.desktop.window_manager import window_state
        states = [window_state(w["hwnd"]) for w in count_windows("Code")]
        return any(not s["iconic"] for s in states)

    results["restaura_ele"]["effect_verified"] = wait_for(code_not_iconic, 5)
    results["traz_ele_pra_frente"] = run_command("traz ele pra frente", expect_fast=True)

    def code_foreground() -> bool:
        from app.desktop.window_manager import window_state
        return any(window_state(w["hwnd"])["foreground"] for w in count_windows("Code"))

    results["traz_ele_pra_frente"]["effect_verified"] = wait_for(code_foreground, 5)
    results["maximiza_ele"] = run_command("maximiza ele", expect_fast=True)

    def code_zoomed() -> bool:
        from app.desktop.window_manager import window_state
        return any(window_state(w["hwnd"])["zoomed"] for w in count_windows("Code"))

    results["maximiza_ele"]["effect_verified"] = wait_for(code_zoomed, 5)
    results["fecha_ele"] = run_command("fecha ele", expect_fast=True)
    results["fecha_ele"]["effect_verified"] = wait_for(lambda: not count_windows("Code"), 8)

    # ---------------------------------------------------------- §23 opens
    for key, cmd, hint in (
        ("open_discord", "abre o Discord", "Discord"),
        ("open_calculator", "abre a Calculadora", "Calculadora"),
        ("open_edge", "abre o Edge", "Edge"),
        ("open_paint", "abre o Paint", "Paint"),
        ("open_powershell", "abre o PowerShell", "Windows PowerShell"),
        ("open_taskmgr", "abre o Gerenciador de Tarefas", "Gerenciador de Tarefas"),
    ):
        result = run_command(cmd, expect_fast=True)
        found = wait_for(lambda h=hint: bool(count_windows(h.split()[0], h)), 14)
        result["effect_verified"] = found
        results[key] = result
        # fecha o que abrimos para não poluir o desktop (apenas hwnd novos)
        for w in count_windows(hint.split()[0], hint):
            if w["hwnd"] not in pre_existing:
                try:
                    from app.desktop import window_manager as wm
                    wm.graceful_close(w["hwnd"], timeout_seconds=3)
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(0.6)

    # ---------------------------------------------------------- §28 folders
    for key, cmd, title in (
        ("folder_downloads", "abre a pasta Downloads", "Downloads"),
        ("folder_documentos", "abre Documentos", "Documentos"),
        ("folder_imagens", "abre Imagens", "Imagens"),
    ):
        result = run_command(cmd, expect_fast=True)
        result["effect_verified"] = wait_for(lambda t=title: bool(count_windows("explorer", t)), 12)
        results[key] = result
    results["folder_fecha_ela"] = run_command("fecha ela", expect_fast=True)
    results["folder_fecha_ela"]["effect_verified"] = wait_for(
        lambda: not count_windows("explorer", "Imagens"), 8)

    # ---------------------------------------------------------- §29 file
    fixture = DATA_ROOT / "kazumi-open-test.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("kazumi-full §29 fixture\n", encoding="utf-8")
    result = run_command("abre o arquivo kazumi-open-test.txt", expect_fast=True)
    result["effect_verified"] = wait_for(
        lambda: any("kazumi-open-test" in w["title"].casefold() for w in windows_now()), 14)
    results["open_file_fixture"] = result
    for w in windows_now():
        if "kazumi-open-test" in w["title"].casefold() and w["hwnd"] not in pre_existing:
            from app.desktop import window_manager as wm
            wm.graceful_close(w["hwnd"], timeout_seconds=3)

    # ---------------------------------------------------------- §30 failure honesty
    results["zombie_honest"] = run_command("abre o zumbi quantum editor 3000", expect_fast=True)
    results["zombie_honest"]["grounded"] = (
        "não encontrei" in results["zombie_honest"]["reply"].casefold()
        and "nada foi executado" in results["zombie_honest"]["reply"].casefold()
    )

    # ---------------------------------------------------------- §13 LIST vs OPEN
    results["list_files_route"] = run_command("mostra os arquivos de downloads")

    # ---------------------------------------------------------- cleanup notepad/explorers criados
    for w in count_windows("notepad"):
        if w["hwnd"] not in pre_existing:
            from app.desktop import window_manager as wm
            wm.graceful_close(w["hwnd"], timeout_seconds=3)
    for w in count_windows("explorer"):
        if w["hwnd"] not in pre_existing and w["title"]:
            from app.desktop import window_manager as wm
            wm.graceful_close(w["hwnd"], timeout_seconds=3)

    summary = {"runtime": runtime, "results": results}
    out = REPORT_ROOT / "e2e-universal-runtime-summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SUMMARY:", out)
    # Restaura o estado original do microfone da janela real.
    try:
        request = urllib.request.Request(
            BASE + "/api/listening/settings", method="PUT",
            data=json.dumps({"enabled": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as resp:
            resp.read()
        print("listening restored")
    except Exception as exc:  # noqa: BLE001
        print(f"listening restore failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
