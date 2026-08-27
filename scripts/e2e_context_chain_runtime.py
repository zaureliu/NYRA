"""§24/§25 context chain no runtime REAL, com dump por passo."""
import json
import sys
import time
import urllib.request

sys.path.insert(0, 'backend')
from app.desktop.windows import annotate_process_names, list_visible_windows  # noqa: E402
from app.desktop.window_manager import window_state  # noqa: E402

BASE = "http://127.0.0.1:8000"


def chat(message):
    req = urllib.request.Request(BASE + "/api/chat", method="POST",
                                 data=json.dumps({"message": message, "synthesize": False}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode())
            return d.get("response", ""), None
    except Exception as exc:
        return "", str(exc)


def code_windows():
    out = []
    for w in annotate_process_names(list_visible_windows()):
        proc = (w.process_name or "").casefold().removesuffix(".exe")
        title = (w.title or "").casefold()
        if proc == "code" or "visual studio code" in title:
            st = window_state(w.hwnd)
            out.append((w.hwnd, w.pid, st["iconic"], st["zoomed"], st["foreground"], w.title[:50]))
    return out


def step(label, message):
    reply, err = chat(message)
    print(f"\n>>> {label}: {message}")
    print(f"    reply: {reply or err}")
    time.sleep(1.2)
    wins = code_windows()
    for hwnd, pid, icon, zoom, fg, title in wins:
        print(f"    CODE hwnd={hwnd} pid={pid} icon={icon} zoom={zoom} fg={fg} | {title}")
    if not wins:
        print("    CODE: nenhuma janela")
    return wins


# silencia voz da janela real
req = urllib.request.Request(BASE + "/api/listening/settings", method="PUT",
                             data=json.dumps({"enabled": False}).encode(),
                             headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=15).read()

step("abrir", "abre o Code")
step("minimizar", "minimiza ele")
step("restaurar", "restaura ele")
step("focar", "traz ele pra frente")
step("maximizar", "maximiza ele")
step("fechar", "fecha ele")

# §25 Discord
def discord_windows():
    out = []
    for w in annotate_process_names(list_visible_windows()):
        proc = (w.process_name or "").casefold().removesuffix(".exe")
        if proc == "discord":
            st = window_state(w.hwnd)
            out.append((w.hwnd, st["iconic"], st["foreground"], w.title[:40]))
    return out

print("\n=== DISCORD ===")
r, e = chat("abre o Discord")
print("abre:", r or e)
time.sleep(6)
print("discord:", discord_windows())
r, e = chat("minimiza ele"); print("minimiza:", r or e); time.sleep(1.5); print("state:", discord_windows())
r, e = chat("traz ele de volta"); print("volta:", r or e); time.sleep(1.5); print("state:", discord_windows())
r, e = chat("fecha ele"); print("fecha:", r or e); time.sleep(2.5); print("state:", discord_windows())

# reativa voz
req = urllib.request.Request(BASE + "/api/listening/settings", method="PUT",
                             data=json.dumps({"enabled": True}).encode(),
                             headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=15).read()
    print("\nlistening restored")
except Exception as exc:
    print("restore fail", exc)
