"""nyra-7c FASE H — E2E cross-layer no runtime REAL (janela + /api/chat).

Cenários §86 percepção, §87 contexto, §89 multi-step, §90/§91 usage,
§92 candidate, §93 execução de skill, §94 falha controlada,
§96 FULL_LOCAL_OPERATOR.
"""
import json
import atexit
import sys
import time
import urllib.request
from pathlib import Path

from runtime_paths import REPORT_ROOT, ensure_script_directories

REPO = Path(__file__).resolve().parents[1]
ensure_script_directories()
sys.path.insert(0, str(REPO / "backend"))
BASE = "http://127.0.0.1:8000"


def http(method: str, path: str, payload=None, timeout=150):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def chat(m):
    return http("POST", "/api/chat", {"message": m, "synthesize": False}).get("response", "")


def wins(proc_hint, title_hint=""):
    from app.desktop.windows import annotate_process_names, list_visible_windows
    out = []
    for w in annotate_process_names(list_visible_windows()):
        p = (w.process_name or "").casefold().removesuffix(".exe")
        t = (w.title or "").casefold()
        if proc_hint in p and (not title_hint or title_hint in t):
            out.append((w.hwnd, w.pid, w.title[:50]))
    return out


results = []
run_id = str(int(time.time()))
desktop_dir = Path.home() / "Desktop"
fixture = desktop_dir / f"nyra-autonomia-7c-{run_id}.txt"
initial_hwnds: dict[str, set[int]] = {}
cleanup_done = False


def record(name, ok, evidence):
    results.append((name, "PASS" if ok else "FAIL", evidence))
    safe = f"{name} :: {evidence}".encode("ascii", "replace").decode()
    print(f"[{'PASS' if ok else 'FAIL'}] {safe[:130]}", flush=True)


def cleanup():
    global cleanup_done
    if cleanup_done:
        return
    cleanup_done = True
    try:
        fixture.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        from app.desktop.window_manager import graceful_close

        for process in ("notepad", "code", "explorer"):
            baseline = initial_hwnds.get(process, set())
            for hwnd, _pid, _title in wins(process):
                if hwnd not in baseline:
                    graceful_close(hwnd)
    except Exception:
        pass
    try:
        http("PUT", "/api/listening/settings", {"enabled": True}, timeout=10)
    except Exception:
        pass


for process in ("notepad", "code", "explorer"):
    initial_hwnds[process] = {item[0] for item in wins(process)}
atexit.register(cleanup)

http("PUT", "/api/listening/settings", {"enabled": False})

# ---------------- §86 percepção + §87 cadeia de contexto (bloco de notas)
r = chat("abre o bloco de notas")
time.sleep(2)
np = wins("notepad")
record("§86/87 abre notepad", bool(np), f"{r[:60]} | {np[:1]}")
r = chat("minimiza ele"); time.sleep(1.5)
np_iconic = wins("notepad")
from app.desktop.window_manager import window_state
minimized_verified = bool(np_iconic) and all(window_state(item[0]).get("iconic")
                                             for item in np_iconic)
r2 = chat("traz ele de volta"); time.sleep(1.5)
np_back = wins("notepad")
restored_verified = bool(np_back) and all(not window_state(item[0]).get("iconic")
                                         for item in np_back)
record("§87 minimiza ele", minimized_verified, r)
record("§87 traz ele de volta", restored_verified, r2)
state = http("GET", "/api/computer/state")
last_target = state["slots"].get("last_target", {}).get("value", {})
record("§17/18 state last_target notepad",
       last_target.get("display_name") == "bloco de notas", last_target)
r = chat("fecha ele"); time.sleep(2.5)
gone = not wins("notepad")
record("§87 fecha ele", gone, r)

# ---------------- §89 multi-step PLAN
r = chat("abre o bloco de notas, escreve 'NYRA teste de autonomia' "
         f"e salva como {fixture.name} e fecha")
deadline = time.time() + 25
ok_file = False
while time.time() < deadline and not ok_file:
    ok_file = fixture.exists() and "NYRA teste de autonomia" in \
        fixture.read_text(encoding="utf-8", errors="ignore")
    time.sleep(0.6)
closed_after_plan = not wins("notepad")
record("§89 multi-step abrir+escrever+salvar+fechar",
       ok_file and closed_after_plan,
       {"reply": r[:100], "file": ok_file, "closed": closed_after_plan})
try:
    fixture.unlink()
except OSError:
    pass

# ---------------- §90/§91/§92 usage learning → alias + workflow candidate
before_stats = http("GET", "/api/computer/usage/stats")
before_workflows = {key: value.get("success_count", 0)
                    for key, value in before_stats["workflow_candidates"].items()}
for i in range(3):
    chat("abre o vscode"); time.sleep(1.2)
    chat("abre a pasta Downloads"); time.sleep(1.2)
stats = http("GET", "/api/computer/usage/stats")
cands = [c for c in stats["workflow_candidates"].values()
         if c.get("success_count", 0) >= 3 and
         c.get("success_count", 0) > before_workflows.get(c["workflow_id"], 0)]
record("§90/§92 workflow candidate criado", bool(cands),
       [(c["workflow_id"], c["steps"], c["confidence"]) for c in cands][:1])
alias = stats["aliases"].get("app:vscode", {})
record("§91 alias aprendido só após efeitos verificados",
       alias.get("successes", 0) >= 3 and alias.get("confidence", 0) >= 0.6,
       {key: alias.get(key) for key in ("canonical", "successes", "confidence")})
skills = http("GET", "/api/computer/skills")["skills"]
candidate_ids = {c["workflow_id"] for c in cands}
cand_skills = [s for s in skills if s["source_workflow_id"] in candidate_ids]
clean_steps = bool(cand_skills) and all(
    ":" not in step["target"] for skill_item in cand_skills
    for step in skill_item["steps"])
record("§92 skill CANDIDATE derivada", bool(cand_skills) and clean_steps,
       [(s["name"], s["state"]) for s in cand_skills][:2])

# fecha o que abrimos na sequência
chat("fecha o code"); time.sleep(2)

# ---------------- §93 skill explícita → execução natural
skill = http("POST", "/api/computer/skills/explicit", {
    "name_hint": f"modo nyra trabalho {run_id}",
    "aliases": [f"modo nyra trabalho {run_id}"],
    "steps": [
        {"capability": "open_folder", "target": "downloads"},
        {"capability": "focus_app", "target": "explorador de arquivos"},
    ],
})
promoted = http("POST", f"/api/computer/skills/{skill['skill_id']}/promote", {})
r = chat(f"modo nyra trabalho {run_id}")
time.sleep(2)
explorer_open = bool(wins("explorer", ""))
record("§93 skill aprendida executada por trigger natural",
       promoted.get("state") == "LEARNED" and explorer_open, r[:100])

# ---------------- §94 falha controlada (precondição quebrada)
skill2 = http("POST", "/api/computer/skills/explicit", {
    "name_hint": f"fluxo impossivel {run_id}",
    "aliases": [f"fluxo impossivel {run_id}"],
    "preconditions": [{"kind": "app_visible", "value": "zumbi-inexistente-3000"}],
    "steps": [{"capability": "open_app", "target": "code"}],
})
http("POST", f"/api/computer/skills/{skill2['skill_id']}/promote", {})
before_conf = skill2.get("confidence")
r = chat(f"fluxo impossivel {run_id}")
skills_after = {s["name"]: s for s in http("GET", "/api/computer/skills")["skills"]}
after = skills_after.get(f"fluxo_impossivel_{run_id}", {})
degraded = after.get("failure_count", 0) >= 1 and after.get("confidence", 1) < before_conf
no_hallucination = "não" in r.casefold() or "precondição" in r.casefold()
record("§94 precondição falha → sem alucinação + degrada",
       degraded and no_hallucination, r[:100])

# ---------------- §96 FULL_LOCAL_OPERATOR
health = http("GET", "/health")
record("§96 FULL_LOCAL_OPERATOR sem read-only",
       health.get("agent", {}).get("read_only") is False,
       health.get("agent"))
tool_surface = {
    item["name"]: item
    for item in http("GET", "/api/tools")
}
clipboard_tools = {
    name: tool_surface.get(name, {}).get("risk")
    for name in ("clipboard_status", "clipboard_write_text", "clipboard_clear")
}
record(
    "§96 clipboard tipado disponível sem AGENT_READ_ONLY",
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
print("\nlistening restored")
print("\n== RESUMO ==")
for name, status, ev in results:
    safe = f"{status} | {name}".encode("ascii", "replace").decode()
    print(safe)
fails = [r for r in results if r[1] == "FAIL"]
print(f"\nTOTAL: {len(results)} | FAIL: {len(fails)}")
report_path = REPORT_ROOT / "nyra-7c-runtime-e2e.json"
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps({
    "run_id": run_id,
    "results": [{"scenario": name, "result": status, "evidence": evidence}
                for name, status, evidence in results],
    "failures": len(fails),
}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
raise SystemExit(1 if fails else 0)
