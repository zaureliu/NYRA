"""nyra-7c FASE H - paridade critica no backend PyInstaller congelado.

O processo congelado deve estar escutando em 127.0.0.1:8000. O harness usa
apenas a API publica, observa efeitos Win32 a partir do runner e nunca encerra
janelas que ja existiam antes do teste.
"""
from __future__ import annotations

import atexit
import json
import sys
import time
import urllib.request
from pathlib import Path

from runtime_paths import REPORT_ROOT, ensure_script_directories

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
from app.core.paths import DATA_ROOT

BASE = "http://127.0.0.1:8000"
ensure_script_directories()
REPORT = REPORT_ROOT / "nyra-7c-frozen-e2e.json"


def http(method: str, path: str, payload=None, timeout: float = 150):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def chat(message: str) -> str:
    return http(
        "POST", "/api/chat", {
            "message": message,
            "synthesize": False,
            "conversation_id": f"frozen-{run_id}",
        }
    ).get("response", "")


def windows(process_hint: str, title_hint: str = "") -> list[tuple[int, str]]:
    from app.desktop.windows import annotate_process_names, list_visible_windows

    found = []
    for window in annotate_process_names(list_visible_windows()):
        process = (window.process_name or "").casefold().removesuffix(".exe")
        title = (window.title or "").casefold()
        if process_hint in process and (not title_hint or title_hint in title):
            found.append((window.hwnd, window.title[:80]))
    return found


results: list[dict] = []
run_id = str(int(time.time() * 1000))
skill_alias = f"paridade congelado {run_id}"
# O resolvedor de OPEN_FOLDER também procura no DATA_ROOT. Uma subpasta única
# no runtime isolado testa resolução por nome sem escrever em Documents nem
# reutilizar/fechar uma janela preexistente do operador.
fixture_folder = DATA_ROOT / f"nyra7cfrozen{run_id}"
fixture_folder.mkdir()
initial_explorer = {item[0] for item in windows("explorer")}
listening_before: bool | None = None
agent_read_only_before: bool | None = None
cleanup_done = False


def record(scenario: str, passed: bool, evidence) -> None:
    result = "PASS" if passed else "FAIL"
    results.append({"scenario": scenario, "result": result, "evidence": evidence})
    printable = f"{scenario} :: {evidence}".encode("ascii", "replace").decode()
    print(f"[{result}] {printable[:180]}", flush=True)


def cleanup() -> None:
    global cleanup_done
    if cleanup_done:
        return
    cleanup_done = True
    try:
        from app.desktop.window_manager import graceful_close

        for hwnd, _title in windows("explorer"):
            if hwnd not in initial_explorer:
                graceful_close(hwnd)
    except Exception:
        pass
    try:
        fixture_folder.rmdir()
    except OSError:
        pass
    if listening_before is not None:
        try:
            http(
                "PUT",
                "/api/listening/settings",
                {"enabled": listening_before},
                timeout=10,
            )
        except Exception:
            pass
    if agent_read_only_before is not None:
        try:
            http(
                "PUT",
                "/api/settings/v3",
                {"key": "agent_read_only", "value": agent_read_only_before},
                timeout=10,
            )
        except Exception:
            pass


atexit.register(cleanup)

settings = http("GET", "/api/listening/settings", timeout=15)
listening_before = bool(settings.get("enabled", True))
http("PUT", "/api/listening/settings", {"enabled": False}, timeout=15)
runtime_settings = http("GET", "/api/settings/v3", timeout=15).get("settings", [])
agent_read_only_before = next(
    (
        bool(item.get("current"))
        for item in runtime_settings
        if item.get("key") == "agent_read_only"
    ),
    True,
)
http(
    "PUT",
    "/api/settings/v3",
    {"key": "agent_read_only", "value": False},
    timeout=15,
)

# Contexto e efeito verificado, sem depender do LLM externo. A pasta única
# impede que "fecha ela" atinja uma janela do operador já aberta no baseline.
reply = chat(f"abre a pasta {fixture_folder.name}")
time.sleep(2)
folder_windows = windows("explorer", fixture_folder.name.casefold())
record(
    "frozen abre pasta fixture",
    bool(folder_windows) and fixture_folder.name.casefold() in reply.casefold(),
    {"reply": reply[:100], "windows": folder_windows[:2]},
)

if folder_windows:
    reply = chat("fecha ela")
    time.sleep(2)
    fixture_closed = not [
        item for item in windows("explorer", fixture_folder.name.casefold())
        if item[0] not in initial_explorer
    ]
else:
    reply = "close skipped because the fixture was not opened"
    fixture_closed = False
record(
    "frozen resolve e fecha referencia",
    fixture_closed and "fech" in reply.casefold(),
    reply[:120],
)

# Skill persistida e disparada por linguagem natural no binario congelado.
skill = http(
    "POST",
    "/api/computer/skills/explicit",
    {
        "name_hint": skill_alias,
        "aliases": [skill_alias],
        "steps": [{"capability": "open_folder", "target": "documentos"}],
    },
)
promoted = http("POST", f"/api/computer/skills/{skill['skill_id']}/promote", {})
reply = chat(skill_alias)
time.sleep(2)
documents = windows("explorer", "document")
record(
    "frozen skill natural verificada",
    promoted.get("state") == "LEARNED"
    and bool(documents)
    and "verific" in reply.casefold(),
    {"reply": reply[:100], "windows": documents[:2]},
)

# Honestidade para target inexistente e exposicao do Computer State.
reply = chat("abre o zumbi quantum 3000")
record(
    "frozen NOT_FOUND honesto",
    ("nao encontrei" in reply.casefold() or "não encontrei" in reply.casefold())
    and "nada foi executado" in reply.casefold(),
    reply[:140],
)

state = http("GET", "/api/computer/state")
slots = state.get("slots", {})
record(
    "frozen computer state exposto",
    "last_target" in slots and "last_successful_action" in slots,
    {"slot_keys": sorted(slots)},
)

skills_after = {
    item["skill_id"]: item for item in http("GET", "/api/computer/skills")["skills"]
}
persisted = skills_after.get(skill["skill_id"], {})
record(
    "frozen skill sem degradacao espuria",
    persisted.get("success_count", 0) >= 1
    and persisted.get("failure_count", 0) == 0,
    {
        "success_count": persisted.get("success_count"),
        "failure_count": persisted.get("failure_count"),
        "confidence": persisted.get("confidence"),
    },
)

health = http("GET", "/health")
record(
    "frozen FULL_LOCAL_OPERATOR",
    health.get("agent", {}).get("read_only") is False,
    health.get("agent"),
)
tool_surface = {
    item["name"]: item
    for item in http("GET", "/api/tools")
}
clipboard_tools = {
    name: tool_surface.get(name, {}).get("risk")
    for name in ("clipboard_status", "clipboard_write_text", "clipboard_clear")
}
record(
    "frozen clipboard tipado sem AGENT_READ_ONLY",
    health.get("agent", {}).get("read_only") is False
    and clipboard_tools == {
        "clipboard_status": "READ_ONLY",
        "clipboard_write_text": "LOW_RISK",
        "clipboard_clear": "LOW_RISK",
    },
    {
        "agent_read_only": health.get("agent", {}).get("read_only"),
        "tools": clipboard_tools,
        "clipboard_content_touched": False,
    },
)

cleanup()
failures = [item for item in results if item["result"] == "FAIL"]
REPORT.parent.mkdir(parents=True, exist_ok=True)
REPORT.write_text(
    json.dumps(
        {"run_id": run_id, "results": results, "failures": len(failures)},
        ensure_ascii=False,
        indent=2,
        default=str,
    ),
    encoding="utf-8",
)
print(f"TOTAL {len(results) - len(failures)}/{len(results)}", flush=True)
print(f"report={REPORT}", flush=True)
raise SystemExit(1 if failures else 0)
