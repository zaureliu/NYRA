"""Fase B — paridade do backend CONGELADO (nyra-backend.exe empacotado).

O desktop real spawnou o backend congelado da própria resource
(backend-runtime). Esta bateria valida o operador universal NELE.
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, 'backend')
from app.desktop.windows import annotate_process_names, list_visible_windows  # noqa: E402
from app.desktop.window_manager import window_state  # noqa: E402

BASE = "http://127.0.0.1:8000"


def chat(m, t=150):
    r = urllib.request.Request(BASE + "/api/chat", method="POST",
                               data=json.dumps({"message": m, "synthesize": False}).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=t) as resp:
        return json.loads(resp.read().decode()).get("response", "")


def dump(hint_proc, hint_title):
    out = []
    for w in annotate_process_names(list_visible_windows()):
        proc = (w.process_name or "").casefold().removesuffix(".exe")
        title = (w.title or "")
        if (hint_proc and hint_proc in proc) or (hint_title and hint_title.casefold() in title.casefold()):
            st = window_state(w.hwnd)
            out.append((w.hwnd, st["iconic"], st["zoomed"], st["foreground"], title[:45]))
    return out


urllib.request.urlopen(urllib.request.Request(
    BASE + "/api/listening/settings", method="PUT",
    data=json.dumps({"enabled": False}).encode(),
    headers={"Content-Type": "application/json"}), timeout=15).read()

print("== FROZEN RUNTIME BATTERY ==", flush=True)
r = chat("abre a pasta Downloads")
ok_dl = any("downloads" in t[4].casefold() for t in dump("explorer", ""))
print(f"[{'PASS' if ok_dl else 'FAIL'}] Downloads -> {r} | explorer={dump('explorer','')[:2]}", flush=True)
r = chat("fecha ela"); time.sleep(2)
gone = not dump("explorer", "Downloads")
print(f"[{'PASS' if gone else 'WARN'}] fecha ela -> {r}", flush=True)

r = chat("abre o Code"); time.sleep(6)
cw = dump("code", "visual studio")
print(f"[{'PASS' if cw else 'FAIL'}] abre Code -> {r} | {cw[:1]}", flush=True)
r = chat("minimiza ele"); time.sleep(1.5); s1 = dump("code", "visual studio")
r = chat("restaura ele"); time.sleep(1.5); s2 = dump("code", "visual studio")
r = chat("maximiza ele"); time.sleep(1.5); s3 = dump("code", "visual studio")
r = chat("traz ele pra frente"); time.sleep(1.5); s4 = dump("code", "visual studio")
min_ok = bool(s1) and all(x[1] for x in s1)
res_ok = bool(s2) and not all(x[1] for x in s2)
max_ok = bool(s3) and all(x[2] for x in s3)
fg_ok = bool(s4) and any(x[3] for x in s4)
for name, flag in (("minimiza", min_ok), ("restaura", res_ok), ("maximiza", max_ok), ("foca", fg_ok)):
    print(f"[{'PASS' if flag else 'FAIL'}] {name} (Code)", flush=True)
r = chat("fecha ele"); time.sleep(3); closed = not dump("code", "visual studio")
print(f"[{'PASS' if closed else 'FAIL'}] fecha Code -> {r}", flush=True)

r = chat("abre o Discord"); time.sleep(7)
r = chat("minimiza ele"); time.sleep(1.5)
r = chat("traz ele de volta"); time.sleep(1.5)
back = dump("discord", "")
r = chat("fecha ele"); time.sleep(3)
d_gone = not dump("discord", "")
print(f"[{'PASS' if back else 'WARN'}] Discord contexto de volta | janelas={back[:1]}", flush=True)
print(f"[{'PASS' if d_gone else 'WARN'}] Discord fechado (tray)", flush=True)

r = chat("abre o zumbi quantum editor 3000")
grounded = "não encontrei" in r.casefold() and "nada foi executado" in r.casefold()
print(f"[{'PASS' if grounded else 'FAIL'}] NOT_FOUND honesto -> {r[:80]}", flush=True)

urllib.request.urlopen(urllib.request.Request(
    BASE + "/api/listening/settings", method="PUT",
    data=json.dumps({"enabled": True}).encode(),
    headers={"Content-Type": "application/json"}), timeout=15).read()
print("listening restored")
