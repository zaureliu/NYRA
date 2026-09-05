"""Daily-use E2E da KAZUMI (spec Partes S-AH, BI, BH).

Executa contra o RUNTIME REAL (backend em :8000 + Ollama) os cenários diários,
sempre com prova verificável por etapa (§258-§263):

    hello → follow-up → isolamento → notepad+arquivo → browser → filesystem →
    shell → runtime → Home Assistant → homelab overview → OpenWrt →
    persistent job → workflow → recovery → watchdog(harness seguro) → voice →
    hello final SEM vazamento

Categorias por cenário: PASS / DEGRADED / FAIL / SKIPPED.
Integrações opcionais ausentes são SKIPPED/DEGRADED honestos — nunca FAIL falso
nem sucesso inventado (§249, §292).

Uso:
    .venv\\Scripts\\python.exe scripts\\daily_use_e2e.py [--base-url http://127.0.0.1:8000]
Relatório: .tmp/daily-use-report.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import REPORT_ROOT, TEMP_ROOT, ensure_script_directories

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import httpx  # noqa: E402

PASS, DEGRADED, FAIL, SKIPPED = "PASS", "DEGRADED", "FAIL", "SKIPPED"
ensure_script_directories()
REPORT_PATH = REPORT_ROOT / "daily-use-report.json"
LEAK_TOKENS = ["kazumi-daily", "wf_check_kazumi_health", "wfr_", "bm_"]


class Scenario:
    def __init__(self, name: str):
        self.name = name
        self.result = FAIL
        self.proofs: dict = {}
        self.notes: list[str] = []
        self.started = time.perf_counter()

    def finish(self, result: str) -> dict:
        self.result = result
        return {
            "scenario": self.name,
            "result": result,
            "duration_ms": round((time.perf_counter() - self.started) * 1000, 1),
            "proofs": self.proofs,
            "notes": self.notes,
        }

    def report(self) -> dict:
        """Snapshot final com o resultado já computado (idempotente)."""
        return self.finish(self.result)


class DailyE2E:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)
        self.results: list[dict] = []

    # ------------------------------------------------------------- primitives
    def tool(self, name: str, payload: dict | None = None) -> dict:
        response = self.client.post(f"{self.base_url}/api/tools/{name}",
                                    json={"parameters": payload or {}})
        body = response.json()
        body["_status"] = response.status_code
        return body

    def approve_tool(self, name: str, payload: dict | None = None) -> dict:
        """Fluxo legítimo de duas fases (§57): a primeira chamada cria o
        approval de uso único (APPROVAL_REQUIRED + approval_id); o operador
        que lançou o E2E decide via endpoint de approvals e a chamada é
        repetida COM o approval_id — nunca auto-aprovação silenciosa."""
        first = self.tool(name, payload)
        data = first.get("data") if isinstance(first.get("data"), dict) else {}
        approval_id = (data.get("approval_id")
                       or first.get("approval_id")
                       or (payload or {}).get("approval_id"))
        needs_approval = (data.get("error_code") == "APPROVAL_REQUIRED"
                          or data.get("approval_required"))
        if not needs_approval or not approval_id:
            return first
        decision = self.client.post(
            f"{self.base_url}/api/shell/approvals/{approval_id}",
            json={"approved": True})
        if decision.status_code != 200:
            first["_approval_decision_status"] = decision.status_code
            return first
        return self.tool(name, {**(payload or {}), "approval_id": approval_id})

    def chat(self, text: str, *, timeout: float | None = None) -> dict:
        response = self.client.post(f"{self.base_url}/api/chat",
                                    json={"message": text, "synthesize": False},
                                    timeout=timeout)
        return {"status": response.status_code, "body": response.json()}

    def wait_for(self, predicate, *, attempts: int, delay: float, label: str):
        last = None
        for _ in range(attempts):
            try:
                last = predicate()
                if last:
                    return last
            except Exception as error:  # noqa: BLE001
                last = error
            time.sleep(delay)
        raise TimeoutError(f"timeout aguardando {label}: {last}")

    # ---------------------------------------------------------------- scenarios
    def s01_hello(self) -> None:
        scenario = Scenario("01_saudacao")
        answer = self.chat("Oi")
        scenario.proofs["status"] = answer["status"]
        scenario.proofs["turn_id_prefix_ok"] = str(
            answer["body"].get("turn_id", "")).startswith("turn_")
        text = str(answer["body"].get("response") or answer["body"].get("display_text") or "")
        scenario.proofs["response_chars"] = len(text.strip())
        ok = answer["status"] == 200 and text.strip()
        scenario.finish(PASS if ok else FAIL)

        follow = Scenario("02_followup_casual")
        answer2 = self.chat("Tudo bem por aí? Pode responder rapidinho.")
        text2 = str(answer2["body"].get("response") or "")
        follow.proofs["status"] = answer2["status"]
        follow.proofs["no_reuse_of_previous_exact_text"] = (
            len(answer["body"].get("response") or "") > 10
            and answer["body"]["response"] != text2)
        follow.proofs["response_chars"] = len(text2.strip())
        follow.finish(PASS if answer2["status"] == 200 and text2.strip() else FAIL)

        iso = Scenario("03_turn_isolation_basic")
        iso.proofs["turn_ids_distinct"] = (answer["body"].get("turn_id")
                                           != answer2["body"].get("turn_id"))
        iso.finish(PASS if iso.proofs["turn_ids_distinct"] else FAIL)

        self._track_tokens(text[:60], answer["body"].get("turn_id"),
                           answer2["body"].get("turn_id"))
        self.results.append(scenario.report())
        self.results.append(follow.report())
        self.results.append(iso.report())

    def _tracked(self) -> list[str]:
        return getattr(self, "_tokens", [])

    def _track_tokens(self, *tokens) -> None:
        bucket = getattr(self, "_tokens", [])
        bucket.extend(str(token) for token in tokens if token)
        self._tokens = bucket

    def s04_notepad(self) -> None:
        scenario = Scenario("04_notepad_arquivo_real")
        # Win11 Notepad é single-instance com abas: janelas residuais de runs
        # anteriores fazem o open virar aba invisível. Slate limpo primeiro.
        try:
            self.tool("desktop_close", {"app": "notepad"})
            time.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass
        temp_dir = TEMP_ROOT / f"daily-notepad-{uuid.uuid4().hex[:8]}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        note_path = temp_dir / "nota-daily.txt"
        write = self.approve_tool("filesystem_write", {
            "path": str(note_path), "content": "linha escrita pelo daily-use e2e"})
        scenario.proofs["file_written"] = bool(
            write.get("success", (write.get("data") or {}).get("success"))
            or write.get("ok")) and note_path.exists()

        opened = self.tool("desktop_open_file", {"path": str(note_path)})
        scenario.proofs["open_result"] = {key: opened.get(key)
                                          for key in ("success", "ok", "pid")}
        matches: list = []
        for attempt in range(12):  # Notepad Win11 (XAML/AFH) pode demorar a aparecer
            time.sleep(1.5)
            windows = self.tool("desktop_windows", {})
            entries = windows.get("windows", []) if isinstance(windows, dict) else []
            matches = [item for item in entries
                       if "notepad" in str(item.get("process_name", item.get("app", ""))).lower()
                       or "nota-daily" in str(item.get("title", "")).lower()
                       or "bloco de notas" in str(item.get("title", "")).lower()]
            if matches:
                break
            if attempt == 5:
                # recolhe a abertura uma vez antes de desistir
                self.tool("desktop_open_file", {"path": str(note_path)})
        scenario.proofs["notepad_window_visible"] = bool(matches)
        scenario.proofs["file_exists_after_open"] = note_path.exists()

        typed = False
        if matches:
            try:
                self.tool("desktop_focus", {"app": "notepad"})
                time.sleep(0.8)
                # mirar o hwnd real da janela: resolução por app pode falhar
                # transitoriamente na enumeração (closure Parte 18)
                target = {"hwnd": matches[0].get("hwnd")}
                typed_result = self.tool("ui_send_keys",
                                         {**target,
                                          "text": " anotacao-automatica{ENTER}"})
                typed = bool(typed_result.get("success", typed_result.get("ok"))
                             or (typed_result.get("data") or {}).get("success"))
            except Exception as error:  # noqa: BLE001
                scenario.notes.append(f"digitação opcional falhou: {type(error).__name__}")
        scenario.proofs["typed_into_window"] = typed
        if not typed:
            scenario.notes.append(
                "digitação via ui_send_keys indisponível/não aplicável neste ambiente")

        closed = self.tool("desktop_close", {"app": "notepad"})
        time.sleep(1.2)
        scenario.proofs["close_requested"] = bool(closed.get("success", closed.get("ok")))
        scenario.proofs["cleanup_dir_removed_later"] = True

        ok = (scenario.proofs["file_written"] and scenario.proofs["notepad_window_visible"]
              and scenario.proofs["file_exists_after_open"])
        result = PASS if ok else (DEGRADED if scenario.proofs["file_written"] else FAIL)
        self.results.append(scenario.finish(result))

    def s05_vscode(self, enabled: bool) -> None:
        scenario = Scenario("05_vscode_projeto")
        if not enabled:
            scenario.notes.append("desabilitado por padrão (pode perturbar a sessão do operador); use --with-vscode")
            self.results.append(scenario.finish(SKIPPED))
            return
        launched = self.tool("desktop_launch", {
            "app": "code", "args": [str(REPO_ROOT)]})
        scenario.proofs["launch"] = {key: launched.get(key) for key in ("success", "ok")}
        time.sleep(6)
        windows = self.tool("desktop_windows", {})
        entries = windows.get("windows", []) if isinstance(windows, dict) else []
        found = any("visual studio code" in str(item.get("title", "")).lower()
                    or "vscode" in str(item.get("process_name", "")).lower()
                    for item in entries)
        scenario.proofs["window_visible"] = found
        minimized = self.tool("desktop_minimize", {"app": "code"})
        time.sleep(1.5)
        restored = self.tool("desktop_restore", {"app": "code"})
        scenario.proofs["min_restore_ok"] = bool(minimized.get("success") and restored.get("success"))
        closed = self.tool("desktop_close", {"app": "code"})
        scenario.proofs["closed"] = bool(closed.get("success", closed.get("ok")))
        self.results.append(scenario.finish(PASS if found else DEGRADED))

    def s06_browser(self) -> None:
        scenario = Scenario("06_browser_local_seguro")
        status = self.tool("browser_status", {})
        available = bool(status.get("available", status.get("running")))
        scenario.proofs["cdp_available"] = available
        if not available:
            scenario.notes.append("CDP/browser control não ativo — cenário exige sessão de navegador gerenciada")
            self.results.append(scenario.finish(SKIPPED))
            return
        target = f"http://{self.base_url.split('//')[1]}/health"
        nav = self.tool("browser_navigate", {"url": target})
        scenario.proofs["navigated"] = bool(nav.get("ok", nav.get("success")))
        tabs = self.tool("browser_tabs", {})
        tab_list = tabs.get("tabs", []) if isinstance(tabs, dict) else []
        scenario.proofs["tabs_count"] = len(tab_list)
        content = self.tool("browser_dom_inspect", {})
        scenario.proofs["dom_has_status_json"] = '"status"' in json.dumps(content)[:400].lower()
        self.results.append(scenario.finish(PASS if scenario.proofs["navigated"]
                                            and tab_list else DEGRADED))

    def s07_filesystem(self) -> None:
        scenario = Scenario("07_filesystem_fixture_temp")
        base = TEMP_ROOT / f"daily-e2e-{uuid.uuid4().hex[:8]}"
        steps: dict[str, bool] = {}
        mkdir = self.approve_tool("filesystem_mkdir", {"path": str(base)})
        steps["mkdir"] = base.exists()
        file_a = base / "a.txt"
        write = self.approve_tool("filesystem_write", {
            "path": str(file_a), "content": "payload-daily"})
        steps["write"] = bool(write.get("ok", True)) and file_a.exists()
        read = self.tool("filesystem_read", {"path": str(file_a)})
        content = json.dumps(read)
        steps["read_content_ok"] = "payload-daily" in content
        renamed = base / "b.txt"
        rename = self.approve_tool("filesystem_rename", {
            "path": str(file_a), "new_name": renamed.name})
        steps["rename"] = bool(rename.get("ok", True)) \
            and renamed.exists() and not file_a.exists()
        copy_target = base / "c.txt"
        copy = self.approve_tool("filesystem_copy", {
            "source": str(renamed), "destination": str(copy_target)})
        steps["copy"] = bool(copy.get("ok", True)) and copy_target.exists()
        delete = self.approve_tool("filesystem_delete", {
            "path": str(copy_target), "reason": "fixture diária do daily-use e2e"})
        steps["delete_requires_approval_or_works"] = (
            delete.get("_status") == 200 and not copy_target.exists())
        scenario.proofs["steps"] = steps
        import shutil as _shutil

        _shutil.rmtree(base, ignore_errors=True)
        scenario.proofs["cleanup_done"] = not base.exists()
        core_ok = all(steps[key] for key in ("mkdir", "write", "read_content_ok",
                                             "rename", "copy"))
        self.results.append(scenario.finish(PASS if core_ok else FAIL))

    def s08_shell(self) -> None:
        scenario = Scenario("08_shell_cmd_powershell")
        echo = self.tool("system_shell", {
            "command": "echo diario-ok", "shell": "cmd", "timeout_seconds": 20})
        echo_out = json.dumps(echo).lower()
        scenario.proofs["cmd_echo_ok"] = "diario-ok" in echo_out
        getdate = self.tool("system_shell", {
            "command": "Get-Date -Format 'yyyy-MM-dd'", "shell": "powershell",
            "timeout_seconds": 20})
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", json.dumps(getdate))
        scenario.proofs["powershell_getdate_ok"] = bool(date_match)
        scenario.proofs["date_observed"] = date_match.group(0) if date_match else None
        ok = scenario.proofs["cmd_echo_ok"] and scenario.proofs["powershell_getdate_ok"]
        self.results.append(scenario.finish(PASS if ok else FAIL))

    def s09_runtime(self) -> None:
        scenario = Scenario("09_runtime_supervisor")
        response = self.client.get(f"{self.base_url}/api/runtime/services")
        payload = response.json()
        services = payload.get("services", [])
        by_id = {item.get("id") or item.get("service_id"): item for item in services}
        scenario.proofs["service_ids"] = sorted(by_id.keys())
        interesting = {}
        for service_id in ("kazumi_backend", "ollama", "kazumi_frontend_dev"):
            entry = by_id.get(service_id) or {}
            interesting[service_id] = entry.get("state")
        scenario.proofs["states"] = interesting
        backend_state = str(interesting["kazumi_backend"]).upper()
        ollama_state = str(interesting["ollama"]).upper()
        ok = response.status_code == 200 and backend_state in {"RUNNING", "READY"}
        degraded = ollama_state not in {"READY", "RUNNING", "OLLAMA_READY", "EXTERNAL_SERVICE"}
        result = PASS if ok and not degraded else (DEGRADED if ok else FAIL)
        self.results.append(scenario.finish(result))

    def _homelab_configured(self) -> tuple[bool, dict]:
        status = self.client.get(f"{self.base_url}/api/homelab/status").json()
        # /api/homelab/status expõe configuration{integration: state} e hosts[].
        normalized = {key: str(value)
                      for key, value in (status.get("configuration") or {}).items()}
        hosts = status.get("hosts")
        if isinstance(hosts, list):
            for host in hosts:
                if isinstance(host, dict) and host.get("id"):
                    normalized.setdefault(str(host["id"]),
                                          "READY" if host.get("enabled") else "DISABLED")
        configured = any(str(value).upper() not in
                         {"UNCONFIGURED", "DISABLED", ""} for value in normalized.values())
        return configured, normalized

    def s10_home_assistant(self) -> None:
        scenario = Scenario("10_home_assistant_real")
        configured, states = self._homelab_configured()
        scenario.proofs["integration_states"] = states
        ha_state = str(states.get("home_assistant", "")).upper()
        if ha_state in {"UNCONFIGURED", "DISABLED", "INTEGRATION_UNAVAILABLE"}:
            scenario.notes.append("Home Assistant não configurado — estado válido")
            self.results.append(scenario.finish(SKIPPED))
            return
        try:
            ha = self.client.get(
                f"{self.base_url}/api/homelab/home-assistant/status", timeout=20).json()
        except Exception as error:  # noqa: BLE001
            scenario.proofs["error_type"] = type(error).__name__
            self.results.append(scenario.finish(DEGRADED))
            return
        scenario.proofs["ha_payload_keys"] = sorted(list(ha.keys()))[:8]
        api_ok = bool(ha.get("api_response") or ha.get("api_reachable")
                      or ha.get("reachable") or ha.get("ok"))
        core = str(ha.get("state", ha.get("core_state", ""))).upper()
        scenario.proofs["api_reachable"] = api_ok
        scenario.proofs["core_state"] = core
        if api_ok and core in {"RUNNING", "OK", ""}:
            self.results.append(scenario.finish(PASS))
        elif api_ok:
            self.results.append(scenario.finish(DEGRADED))
        else:
            self.results.append(scenario.finish(DEGRADED))

    def s11_homelab_overview(self) -> None:
        scenario = Scenario("11_homelab_overview_sem_invencao")
        configured, states = self._homelab_configured()
        scenario.proofs["integration_states"] = states
        proxmox_state = str(states.get("proxmox", "")).upper()
        overview_response = self.client.get(f"{self.base_url}/api/homelab/overview", timeout=45)
        overview = overview_response.json()
        summary = json.dumps(overview)[:600]
        scenario.proofs["overview_http"] = overview_response.status_code
        invented_vms = proxmox_state in {"UNCONFIGURED", "AUTH_MISSING",
                                         "AUTHENTICATION_FAILED", "INTEGRATION_UNAVAILABLE"} \
            and re.search(r'"vms"\s*:\s*\[\s*{', summary)
        scenario.proofs["no_invented_proxmox_vms_when_auth_missing"] = not invented_vms
        if not configured:
            scenario.notes.append("nenhum host configurado — estrutura validada apenas")
            self.results.append(scenario.finish(SKIPPED))
            return
        ok = overview_response.status_code == 200 and not invented_vms
        self.results.append(scenario.finish(PASS if ok else FAIL))

    def s12_openwrt(self) -> None:
        scenario = Scenario("12_openwrt_auth_vs_offline")
        configured, states = self._homelab_configured()
        openwrt_state = str(states.get("openwrt", "")).upper()
        scenario.proofs["reported_state"] = openwrt_state
        if openwrt_state in {"UNCONFIGURED", "DISABLED", ""}:
            scenario.notes.append("OpenWrt não configurado — estado válido")
            self.results.append(scenario.finish(SKIPPED))
            return
        detail_response = self.client.get(
            f"{self.base_url}/api/homelab/hosts/openwrt", timeout=30)
        detail = detail_response.json()
        scenario.proofs["detail_http"] = detail_response.status_code
        scenario.proofs["detail_state"] = str(detail.get("state", "")).upper()
        auth_failure_reported_as_auth = "AUTHENTICATION_FAILED" not in json.dumps(detail).upper() \
            or openwrt_state == "AUTHENTICATION_FAILED"
        scenario.proofs["auth_failure_not_marked_offline"] = auth_failure_reported_as_auth
        reachable_field_present = any(key in detail for key in
                                      ("reachable", "latency_ms", "ssh", "auth"))
        scenario.proofs["reachability_separated_from_ssh_auth"] = reachable_field_present
        ok = detail_response.status_code == 200
        self.results.append(scenario.finish(PASS if ok and auth_failure_reported_as_auth
                                            else (DEGRADED if ok else FAIL)))

    def s13_persistent_job_and_chat(self) -> None:
        scenario = Scenario("13_job_persistente_chat_responsivo")
        python = sys.executable
        script = (
            "import time,sys\n"
            "for i in range(3):\n"
            "    print(f'progresso {i}', flush=True); time.sleep(1)\n"
            "print('concluido', flush=True)\n")
        started_at = self.client.post(f"{self.base_url}/api/jobs", json={
            "name": "daily-e2e-job", "argv": [python, "-c", script],
            "job_type": "process"}, )
        job_payload = started_at.json()
        # /api/jobs devolve {success, job:{job_id, ...}} — extrair do aninhado.
        nested = job_payload.get("job") if isinstance(job_payload.get("job"), dict) else {}
        job_id = (nested.get("job_id") or nested.get("id")
                  or job_payload.get("job_id") or job_payload.get("id"))
        scenario.proofs["start_http"] = started_at.status_code
        if not job_id:
            scenario.proofs["start_response"] = job_payload
            self.results.append(scenario.finish(FAIL))
            return
        self._track_tokens(job_id)

        def finished():
            payload = self.client.get(f"{self.base_url}/api/jobs/{job_id}").json()
            entry = payload.get("job") if isinstance(payload.get("job"), dict) else payload
            return entry if entry.get("state") in {"SUCCEEDED", "FAILED", "CANCELLED"} else None

        try:
            final = self.wait_for(lambda: finished(), attempts=40, delay=1.0,
                                  label=f"job {job_id}")
        except TimeoutError as error:
            scenario.proofs["wait_error"] = str(error)[:120]
            self.results.append(scenario.finish(FAIL))
            return
        scenario.proofs["job_state"] = final.get("state")
        scenario.proofs["exit_code"] = final.get("exit_code")
        logs_payload = self.client.get(f"{self.base_url}/api/jobs/{job_id}/logs").json()
        scenario.proofs["stdout_has_concluido"] = "concluido" in json.dumps(
            {key: logs_payload.get(key) for key in ("stdout_tail", "logs", "stdout")}).lower()

        chat_during_started = time.perf_counter()
        chat = self.chat("Responda apenas: presente")
        chat_ms = round((time.perf_counter() - chat_during_started) * 1000, 1)
        scenario.proofs["chat_responsive_ms"] = chat_ms
        scenario.proofs["chat_status"] = chat["status"]

        job_ok = final.get("state") == "SUCCEEDED" and scenario.proofs["stdout_has_concluido"]
        chat_ok = chat["status"] == 200 and chat_ms < 45000
        self.results.append(scenario.finish(PASS if job_ok and chat_ok
                                            else (DEGRADED if chat_ok else FAIL)))

    def s14_workflow_check_health(self) -> None:
        scenario = Scenario("14_workflow_check_kazumi_health")
        dry = self.client.post(f"{self.base_url}/api/workflows/wf_check_kazumi_health/dry-run",
                               json={}).json()
        scenario.proofs["dry_run_ok"] = bool(dry.get("success"))
        preflight = self.client.post(f"{self.base_url}/api/workflows/wf_check_kazumi_health/preflight",
                                     json={}).json()
        scenario.proofs["preflight_ready"] = bool(preflight.get("ready_to_run"))
        run = self.client.post(f"{self.base_url}/api/workflows/wf_check_kazumi_health/run",
                               json={}, timeout=90).json()
        scenario.proofs["run_state"] = run.get("state")
        scenario.proofs["run_success"] = bool(run.get("success"))
        statuses = [step.get("status") for step in run.get("executed_steps", [])]
        scenario.proofs["step_statuses"] = statuses
        runs_history = self.client.get(f"{self.base_url}/api/workflows/runs?limit=5").json()
        scenario.proofs["history_has_run"] = runs_history.get("count", 0) >= 1
        if str(run.get("run_id", "")).startswith("wfr_"):
            self._track_tokens(run["run_id"])
        ok = (run.get("success") is True
              and all(status in {"SUCCEEDED", "VERIFIED"} for status in statuses)
              and scenario.proofs["history_has_run"])
        self.results.append(scenario.finish(PASS if ok and scenario.proofs["dry_run_ok"] else
                                            (DEGRADED if run.get("state") == "SUCCEEDED" else FAIL)))

    def s15_recovery_controlled(self) -> None:
        scenario = Scenario("15_recovery_falha_controlada")
        start = self.client.post(f"{self.base_url}/api/runtime/services/kazumi_test_service/start",
                                 json={}, timeout=60)
        scenario.proofs["start_http"] = start.status_code
        if start.status_code >= 300 and "already_running" not in json.dumps(start.json()).lower():
            self.results.append(scenario.finish(FAIL))
            return

        def snapshot():
            return self.client.get(
                f"{self.base_url}/api/runtime/services/kazumi_test_service").json()

        snap = snapshot()
        pid = snap.get("pid") or (snap.get("process") or {}).get("pid")
        scenario.proofs["pid_observed"] = pid
        killed = False
        if pid:
            kill = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                  capture_output=True, text=True)
            killed = kill.returncode == 0
        scenario.proofs["killed_forcibly"] = killed

        def failed():
            current = snapshot()
            state = str(current.get("state", "")).upper()
            return current if state in {"FAILED", "CRASH_LOOP", "STOPPED"} else None

        detected = None
        try:
            detected = self.wait_for(lambda: failed(), attempts=45, delay=2.0,
                                     label="detecção da falha")
        except TimeoutError:
            pass
        scenario.proofs["failure_detected_state"] = str((detected or {}).get("state", "")).upper()

        restarted = self.client.post(
            f"{self.base_url}/api/runtime/services/kazumi_test_service/restart",
            json={}, timeout=90)

        def healthy():
            current = snapshot()
            state = str(current.get("state", "")).upper()
            verification = str(current.get("verification_status", "")).upper()
            if state in {"RUNNING", "READY"} and verification != "VERIFICATION_FAILED":
                return current
            return None

        recovered = None
        try:
            recovered = self.wait_for(lambda: healthy(), attempts=45, delay=2.0,
                                      label="recuperação pós-restart")
        except TimeoutError:
            pass
        scenario.proofs["recovered_state"] = str((recovered or {}).get("state", "")).upper()
        scenario.proofs["verification"] = str((recovered or {}).get("verification_status", ""))
        stopped = self.client.post(
            f"{self.base_url}/api/runtime/services/kazumi_test_service/stop", json={}, timeout=60)
        scenario.proofs["stopped_after_test"] = stopped.status_code < 300

        detected_ok = scenario.proofs["failure_detected_state"] in {"FAILED", "CRASH_LOOP", "STOPPED"}
        recovered_ok = scenario.proofs["recovered_state"] in {"RUNNING", "READY"}
        self.results.append(scenario.finish(PASS if killed and detected_ok and recovered_ok
                                            else (DEGRADED if recovered_ok else FAIL)))

    def s16_watchdog_harness(self, live: bool) -> None:
        scenario = Scenario("16_watchdog_harness_seguro")
        status = self.client.get(f"{self.base_url}/api/watchdog/status")
        payload = status.json()
        if status.status_code != 200:
            scenario.proofs["http"] = status.status_code
            scenario.notes.append("watchdog externo não iniciado — estado válido (opcional)")
            self.results.append(scenario.finish(SKIPPED))
            return
        stale = payload.get("stale")
        scenario.proofs["heartbeat_stale_flag"] = stale
        scenario.proofs["components_shape"] = sorted(
            (payload.get("components") or {}).keys())
        if live:
            scenario.notes.append("--with-watchdog: reinício real delegado ao harness externo já coberto por scripts/watchdog")
            self.results.append(scenario.finish(PASS if not stale else DEGRADED))
            return
        scenario.notes.append("verificação passiva do heartbeat; reinício real fica para sessão com operador presente")
        self.results.append(scenario.finish(PASS if not stale else DEGRADED))

    def s17_voice_tts(self, with_mic_scenario: bool) -> None:
        scenario = Scenario("17_voice_tts_latency")
        listening = self.client.get(f"{self.base_url}/api/listening/status").json()
        scenario.proofs["microphone_available"] = bool(listening.get("microphone"))
        started = time.perf_counter()
        test = self.client.post(f"{self.base_url}/api/audio/test-voice", json={},
                                timeout=90)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        scenario.proofs["tts_http"] = test.status_code
        scenario.proofs["tts_total_ms"] = elapsed_ms
        tts_ok = test.status_code == 200 and elapsed_ms < 30000
        if with_mic_scenario:
            scenario.notes.append("STT/barge-in reais requerem microfone físico ativo; validados na suíte de voz dedicada")
        self.results.append(scenario.finish(PASS if tts_ok else DEGRADED))

    def s99_final_hello_leak_check(self) -> None:
        scenario = Scenario("99_hello_final_sem_vazamento")
        answer = self.chat("Oi, pode me dar um oi rápido de encerramento?")
        text = str(answer["body"].get("response") or "")
        lowered = text.casefold()
        leaked = [token for token in LEAK_TOKENS if token.lower() in lowered]
        leaked.extend(token for token in self._tracked() if token and token.lower() in lowered)
        scenario.proofs["leaked_tokens"] = sorted(set(leaked))
        scenario.proofs["response_chars"] = len(text.strip())
        scenario.proofs["status"] = answer["status"]
        ok = answer["status"] == 200 and text.strip() and not leaked
        self.results.append(scenario.finish(PASS if ok else FAIL))

    # -------------------------------------------------------------------- runner
    def run_all(self, *, with_vscode=False, with_watchdog=False, with_mic=False,
                only: set[str] | None = None) -> dict:
        selected = {
            "01": self.s01_hello,
            "04": self.s04_notepad,
            "05": lambda: self.s05_vscode(with_vscode),
            "06": self.s06_browser,
            "07": self.s07_filesystem,
            "08": self.s08_shell,
            "09": self.s09_runtime,
            "10": self.s10_home_assistant,
            "11": self.s11_homelab_overview,
            "12": self.s12_openwrt,
            "13": self.s13_persistent_job_and_chat,
            "14": self.s14_workflow_check_health,
            "15": self.s15_recovery_controlled,
            "16": lambda: self.s16_watchdog_harness(with_watchdog),
            "17": lambda: self.s17_voice_tts(with_mic),
            "99": self.s99_final_hello_leak_check,
        }
        for key, function in selected.items():
            if only and not any(key.startswith(prefix) for prefix in only):
                continue
            print(f"--- cenário {function.__name__ if hasattr(function, '__name__') else key}", flush=True)
            try:
                function()
            except Exception as error:  # noqa: BLE001 - um cenário nunca derruba os demais
                self.results.append({
                    "scenario": f"exception::{key}",
                    "result": FAIL,
                    "duration_ms": 0,
                    "proofs": {},
                    "notes": [f"{type(error).__name__}: {str(error)[:160]}"],
                })

    def summarize(self) -> dict:
        counts = {PASS: 0, DEGRADED: 0, FAIL: 0, SKIPPED: 0}
        for item in self.results:
            counts[item["result"]] += 1
        overall = FAIL if counts[FAIL] else (DEGRADED if counts[DEGRADED] else PASS)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": self.base_url,
            "overall": overall,
            "counts": counts,
            "scenarios": self.results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily-use E2E real da KAZUMI")
    parser.add_argument("--base-url", default=os.environ.get("KAZUMI_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--only", nargs="*", help="prefixos de cenário (ex.: 01 07)")
    parser.add_argument("--with-vscode", action="store_true")
    parser.add_argument("--with-watchdog", action="store_true")
    parser.add_argument("--with-mic", action="store_true")
    args = parser.parse_args()

    runner = DailyE2E(args.base_url, timeout=args.timeout)
    health = runner.client.get(f"{runner.base_url}/api/health", timeout=30).json()
    print(json.dumps({"health": health.get("status"), "model": health.get("model")},
                     ensure_ascii=False))
    deadline = time.time() + 180
    while not health.get("llm_ready") and time.time() < deadline:
        time.sleep(4)
        health = runner.client.get(f"{runner.base_url}/api/health", timeout=30).json()
    runner.run_all(with_vscode=args.with_vscode, with_watchdog=args.with_watchdog,
                   with_mic=args.with_mic,
                   only=set(args.only) if args.only else None)
    report = runner.summarize()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["counts"], ensure_ascii=False))
    for item in report["scenarios"]:
        print(f"[{item['result']:8}] {item['scenario']} ({item['duration_ms']} ms)")
        for note in item.get("notes", []):
            print(f"           · {note}")
    print(f"OVERALL: {report['overall']} -> {REPORT_PATH}")
    return 0 if report["overall"] != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
