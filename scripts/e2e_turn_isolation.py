"""NYRA E2E Turn Isolation — real backend, UI HTTP/WS channels, real desktop.

Runs the mandated acceptance sequence over the same channels the operator uses:
  POST /api/chat (HTTP) while listening to /api/ws for turn-tagged events.

Checks per spec prompt6 (#100..#122, #169..#170):
  - each "oi" answer contains none of the previous operational content
  - "abre o bloco de notas" physically opens Notepad with a visible window
  - "o bloco de notas esta aberto?" answers from a live tool observation
  - an app NOT present in the registry is found/launched by dynamic discovery

Exit code 0 = all assertions passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from runtime_paths import REPORT_ROOT, TEMP_ROOT, ensure_script_directories

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
ensure_script_directories()

import httpx  # noqa: E402


TURN_REQUIRED_EVENTS = {"USER_TEXT_RECEIVED", "NYRA_RESPONSE"}


def ws_collect(base_url: str) -> tuple[list[dict], Callable[[], None]]:
    """Return (events_list, stop_fn) collecting WS events in a background thread."""
    import websocket  # type: ignore

    events: list[dict] = []
    ws_url = base_url.replace("http", "ws", 1) + "/api/ws"
    socket = websocket.WebSocket()
    socket.settimeout(0.5)
    socket.connect(ws_url, http_proxy_host=None)
    running = {"flag": True}

    def run() -> None:
        while running["flag"]:
            try:
                raw = socket.recv()
                events.append(json.loads(raw))
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError, ValueError, json.JSONDecodeError):
                break

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def stop() -> None:
        running["flag"] = False
        try:
            socket.close()
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout=2)

    return events, stop


def event_turn_id(event: dict) -> str | None:
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return None
    value = payload.get("turn_id")
    return value if isinstance(value, str) and value.startswith("turn_") else None


async def wait_for_turn_events(events: list[dict], turn_id: str, timeout: float = 4.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matched = [event for event in list(events) if event_turn_id(event) == turn_id]
        if TURN_REQUIRED_EVENTS <= {str(event.get("type")) for event in matched}:
            return matched
        await asyncio.sleep(0.05)
    return [event for event in list(events) if event_turn_id(event) == turn_id]


def win32_windows_for(needles: list[str]) -> list[dict]:
    """Enumerate visible top-level windows matching title/process needles."""
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.windll.user32
    results: list[dict] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def on_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = ""
        try:
            process_name = psutil.Process(int(pid.value)).name().casefold()
        except Exception:  # noqa: BLE001
            pass
        haystack = f"{title.casefold()} {process_name}"
        if any(needle in haystack for needle in needles):
            results.append({"hwnd": int(hwnd), "pid": int(pid.value), "title": title, "process": process_name})
        return True

    procedure = EnumWindowsProc(on_window)
    user32.EnumWindows(procedure, 0)
    return results


async def chat(client: httpx.AsyncClient, message: str) -> dict:
    response = await client.post("/api/chat", json={"message": message, "synthesize": False}, timeout=300)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code != 200:
        return {"_http_status": response.status_code, "_detail": body}
    return body


def unwrap(tool_payload: dict) -> dict:
    """ToolResult envelope {tool,risk,ok,elapsed_ms,data} -> inner data dict."""
    if isinstance(tool_payload, dict) and isinstance(tool_payload.get("data"), dict):
        merged = {"tool_ok": tool_payload.get("ok"), **tool_payload["data"]}
        return merged
    return tool_payload


def assert_clean_greeting(turn_label: str, answer: str, forbidden: list[str]) -> list[str]:
    problems: list[str] = []
    folded = answer.casefold()
    for token in forbidden:
        if token in folded:
            problems.append(f"[{turn_label}] resposta contém conteúdo proibido {token!r}: {answer!r}")
    return problems


def contains_standalone_number(answer: str, number: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", answer) is not None


async def main_async(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    report: dict = {"sequence": [], "checks": {}, "problems": []}
    problems = report["problems"]
    timeout = httpx.Timeout(args.timeout, connect=5)
    ws_events, stop_ws = ws_collect(base_url)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        health = await client.get("/api/health")
        ready = health.json().get("llm_ready")
        print(f"health llm_ready={ready}")
        if not ready:
            print("Aguardando warmup do LLM local...")
            for _ in range(60):
                await asyncio.sleep(5)
                if (await client.get("/api/health")).json().get("llm_ready"):
                    break
            else:
                problems.append("LLM local não ficou ready dentro do timeout de warmup")
        tools_before = (await client.get("/api/tools")).json()
        tool_names = {item["name"] for item in tools_before}
        assert {"desktop_open_application", "desktop_find_application"} <= tool_names, "tools dinâmicas ausentes"

        pre_existing_notepad: list[dict] = []
        if not args.skip_physical_launch:
            pre_existing_notepad = win32_windows_for(["bloco de notas", "notepad"])
            report["checks"]["notepad_windows_before"] = pre_existing_notepad

        # ------------------------------------------------ sequência obrigatória
        sequence = [
            ("2+2", ["4"]),
            ("oi", []),
            ("qual a capital do Brasil?", ["brasília"]),
            ("oi", []),
            ("abre o bloco de notas", []),
            ("oi", []),
            ("o bloco de notas está aberto?", []),
            ("oi", []),
        ]
        history_answers: list[tuple[str, str]] = []
        seen_turn_ids: set[str] = set()
        for index, (message, expected_tokens) in enumerate(sequence):
            started = time.perf_counter()
            result = await chat(client, message)
            elapsed = round(time.perf_counter() - started, 1)
            status = result.get("pipeline_status")
            answer = str(result.get("response") or "")
            turn_id = result.get("turn_id") or ""
            entry = {
                "input": message,
                "turn_id": turn_id,
                "status": status,
                "response": answer[:220],
                "seconds": elapsed,
            }
            report["sequence"].append(entry)
            print(f"\n[{index + 1}/{len(sequence)}] '{message}' -> ({status}, {elapsed}s, {turn_id})\n  NYRA: {answer[:200]}")

            if not turn_id.startswith("turn_"):
                problems.append(f"resposta sem turn_id válido: {result}")
            elif turn_id in seen_turn_ids:
                problems.append(f"turn_id reutilizado entre entradas: {turn_id}")
            else:
                seen_turn_ids.add(turn_id)
            if not answer.strip():
                problems.append(f"resposta vazia para {message!r}")
            folded_answer = answer.casefold()
            for expected in expected_tokens:
                if expected.casefold() not in folded_answer:
                    problems.append(f"[{turn_id}] resposta a {message!r} não contém {expected!r}: {answer!r}")

            turn_events = await wait_for_turn_events(ws_events, turn_id)
            event_types = {str(event.get("type")) for event in turn_events}
            entry["ws_events"] = sorted(event_types)
            entry["agent_tools"] = sorted({
                str(event.get("payload", {}).get("tool"))
                for event in turn_events
                if event.get("type") == "AGENT_RUN_STEP"
                and event.get("payload", {}).get("tool")
            })
            missing_events = TURN_REQUIRED_EVENTS - event_types
            if missing_events:
                problems.append(f"[{turn_id}] eventos WebSocket ausentes: {sorted(missing_events)}")
            response_events = [event for event in turn_events if event.get("type") == "NYRA_RESPONSE"]
            if response_events:
                ws_text = str(response_events[-1].get("payload", {}).get("text") or "")
                if ws_text != answer:
                    problems.append(f"[{turn_id}] NYRA_RESPONSE WS diverge da resposta HTTP")

            # Cada 'oi' não pode conter NENHUMA resposta operacional anterior.
            if message == "oi":
                forbidden = []
                for previous_input, previous_answer in history_answers:
                    if previous_input in {"oi"}:
                        continue
                    if "capital" in previous_input and "brasília" in previous_answer.casefold():
                        forbidden.append("brasília")
                    if "bloco" in previous_input or "notepad" in previous_answer.casefold():
                        forbidden.extend(["bloco de notas", "notepad"])
                if "2+2" in [item[0] for item in history_answers]:
                    if contains_standalone_number(folded_answer, "4"):
                        problems.append(f"[{turn_id}] saudação reutilizou o resultado numérico do turno anterior: {answer!r}")
                for token in ("[short_term", "processo", "serviço", "executando", "rodando", "aberto", "fechado", "pid", "memória"):
                    if token in folded_answer:
                        problems.append(f"[{turn_id}] saudação respondeu com conteúdo técnico não solicitado {token!r}: {answer!r}")
                problems.extend(assert_clean_greeting(message, answer, forbidden))
            history_answers.append((message, answer))
            await asyncio.sleep(0.4)

        notepad_entry = next(item for item in report["sequence"] if item["input"] == "abre o bloco de notas")
        status_entry = next(item for item in report["sequence"] if "está aberto" in item["input"])
        if "DESKTOP_WINDOW_VERIFIED" not in notepad_entry.get("ws_events", []):
            problems.append(f"[{notepad_entry['turn_id']}] abertura não emitiu DESKTOP_WINDOW_VERIFIED correlacionado")
        if not re.search(r"(?i)\b(abert[oa]|confirmad[oa]|janela)\b", notepad_entry["response"]) or re.search(
            r"(?i)\b(não consegui confirmar|não foi abert[oa]|falhou)\b", notepad_entry["response"],
        ):
            problems.append(f"[{notepad_entry['turn_id']}] resposta de abertura não reporta o efeito verificado: {notepad_entry['response']!r}")
        if "desktop_windows" not in status_entry.get("agent_tools", []):
            problems.append(f"[{status_entry['turn_id']}] consulta de status não executou desktop_windows neste turno")
        if not re.search(r"(?i)\b(sim|abert[oa]|janela|executando|rodando)\b", status_entry["response"]):
            problems.append(f"[{status_entry['turn_id']}] resposta de status não confirma observação atual: {status_entry['response']!r}")
        if "[short_term" in status_entry["response"].casefold():
            problems.append(f"[{status_entry['turn_id']}] resposta de status repetiu memória em vez de evidência atual")

        # ------------------------------------------- verificação física Win32
        if not args.skip_physical_launch:
            windows = win32_windows_for(["bloco de notas", "notepad"])
            report["checks"]["notepad_windows"] = windows
            previous_hwnds = {item["hwnd"] for item in pre_existing_notepad}
            new_windows = [item for item in windows if item["hwnd"] not in previous_hwnds]
            report["checks"]["notepad_windows_created"] = new_windows
            print(f"\nwin32 notepad windows: {windows}")
            if not windows:
                problems.append("janela física do Bloco de Notas não encontrada no desktop")

        # estado via tool real (mesma cadeia usada na conversa)
        state_result = unwrap((await client.post(
            "/api/tools/desktop_windows",
            json={"parameters": {"query": "bloco de notas"}},
        )).json())
        report["checks"]["desktop_windows_query"] = state_result
        if not state_result.get("open"):
            problems.append(f"desktop_windows query não confirma janela: {state_result}")

        # ------------------------------------------------- descoberta dinâmica
        find_payload = unwrap((await client.post(
            "/api/tools/desktop_find_application",
            json={"parameters": {"query": args.dynamic_app}},
        )).json())
        report["checks"]["dynamic_find"] = find_payload
        print(f"\ndiscovery[{args.dynamic_app}]: {find_payload.get('status')} "
              f"-> {[c['display_name'] for c in find_payload.get('candidates', [])][:3]}")
        if find_payload.get("status") not in {"EXACT_MATCH", "HIGH_CONFIDENCE"}:
            problems.append(f"descoberta dinâmica falhou para {args.dynamic_app}: {find_payload}")

        if args.launch_dynamic:
            dynamic_launch = unwrap((await client.post(
                "/api/tools/desktop_open_application",
                json={"parameters": {"query": args.dynamic_app}},
            )).json())
            report["checks"]["dynamic_launch"] = {
                key: dynamic_launch.get(key)
                for key in ("success", "error_code", "message", "effect_verified", "verification_status", "pid", "windows")
            }
            print(f"dynamic launch: {report['checks']['dynamic_launch']}")
            if not (dynamic_launch.get("success") and dynamic_launch.get("effect_verified") is True):
                problems.append(f"launch dinâmico não foi fisicamente verificado: {dynamic_launch}")
        else:
            report["checks"]["dynamic_launch"] = {"skipped": True}

        # ------------------------------------------------------ full shell test
        shell_ps = unwrap((await client.post(
            "/api/tools/system_shell",
            json={"parameters": {"command": "Get-Date -Format 'yyyy-MM-dd HH:mm'", "reason": "e2e shell check"}},
        )).json())
        shell_cmd = unwrap((await client.post(
            "/api/tools/system_shell",
            json={"parameters": {"command": "ver", "shell": "cmd"}},
        )).json())
        report["checks"]["shell"] = {"powershell_stdout": shell_ps.get("stdout", "")[:80], "cmd_exit": shell_cmd.get("exit_code")}
        if not shell_ps.get("success") or not shell_cmd.get("success"):
            problems.append(f"shell test falhou: ps={shell_ps.get('error_code')} cmd={shell_cmd.get('error_code')}")

        # --------------------------------------------------- filesystem test
        fs_root = (TEMP_ROOT / f"e2e-fs-{time.time_ns()}").as_posix()
        fs_ops = [
            (f"New-Item -ItemType Directory -Path {fs_root}", True),
            (f"Set-Content -Path {fs_root}/probe.txt -Value 'nyra-e2e'", True),
            (f"Get-Content {fs_root}/probe.txt", True),
            (f"Rename-Item {fs_root}/probe.txt probe2.txt", True),
        ]
        fs_results = []
        for command, expect_success in fs_ops:
            data = unwrap((await client.post(
                "/api/tools/system_shell",
                json={"parameters": {"command": command}},
            )).json())
            fs_results.append({"command": command.split(";")[0][:40], "success": data.get("success")})
            if bool(data.get("success")) is not expect_success:
                problems.append(f"filesystem op falhou: {command} -> {data.get('message')}")
        report["checks"]["filesystem"] = fs_results

        # Exclusão continua approval-gated. O E2E verifica fail-closed e nega a
        # solicitação; ele nunca concede a si próprio autorização destrutiva.
        delete_data = unwrap((await client.post(
            "/api/tools/system_shell",
            json={"parameters": {"command": f"Remove-Item {fs_root}/probe2.txt"}},
        )).json())
        report["checks"]["filesystem_delete_guard"] = {
            "error_code": delete_data.get("error_code"),
            "approval_required": delete_data.get("approval_required"),
        }
        if delete_data.get("error_code") != "APPROVAL_REQUIRED":
            problems.append(f"delete não falhou fechado com approval: {delete_data}")
        elif delete_data.get("approval_id"):
            await client.post(
                f"/api/shell/approvals/{delete_data['approval_id']}",
                json={"approved": False},
            )

        # ------------------------------------------------------------ métricas
        metrics = (await client.get("/api/turns/metrics")).json()["metrics"]
        report["checks"]["turn_metrics"] = metrics
        print(f"\nturn metrics: {metrics}")

        # cleanup do notepad físico aberto pelo teste (somente PIDs com janela nossa)
        if not args.skip_physical_launch and args.close_notepad:
            for window in report["checks"].get("notepad_windows_created", []):
                try:
                    import psutil
                    psutil.Process(window["pid"]).terminate()
                except Exception:  # noqa: BLE001
                    pass

    stop_ws()
    report["checks"]["websocket"] = {
        "connected": any(event.get("type") == "CONNECTED" for event in ws_events),
        "events_collected": len(ws_events),
    }
    if not report["checks"]["websocket"]["connected"]:
        problems.append("WebSocket não confirmou CONNECTED")

    print("\n" + "=" * 70)
    if problems:
        print(f"E2E FALHOU — {len(problems)} problema(s):")
        for problem in problems:
            print(f"  - {problem}")
        (REPORT_ROOT / "e2e-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    print("E2E TURN ISOLATION: TODOS OS CHECKS PASSARAM")
    (REPORT_ROOT / "e2e-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--dynamic-app", default="Git GUI")
    parser.add_argument("--skip-physical-launch", action="store_true")
    parser.add_argument("--launch-dynamic", action="store_true", help="abre e exige verificação física do app dinâmico")
    parser.add_argument("--close-notepad", action="store_true", help="fecha apenas janelas novas criadas por este E2E")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
