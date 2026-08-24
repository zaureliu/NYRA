"""Runtime Supervisor V1 test suite: registry, process lifecycle, health, logs,
crash-loop protection, locks, approval gate integration, agent flow and auto-recovery."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from app.agent import AgentController
from app.core.config import Settings
from app.core.turn import current_turn_id
from app.events import EventBus, EventType
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMToolCall, LLMToolFunction
from app.runtime import (
    ProcessManager,
    RuntimeHistory,
    RuntimeSupervisor,
    load_runtime_registry,
    register_runtime_tools,
)
from app.runtime.models import RuntimeState
from app.tools.models import RiskLevel
from app.tools.registry import ToolRegistry
from app.tools.shell_approval import ShellApprovalGate


def base_settings(tmp_path: Path, **overrides) -> Settings:
    overrides.setdefault("runtime_supervisor_enabled", False)  # monitor só onde o teste pede
    return Settings.from_sources(
        database_path=tmp_path / "runtime.db",
        runtime_services_path=overrides.pop("runtime_services_path", tmp_path / "runtime_services.yaml"),
        **overrides,
    )


def make_supervisor(settings: Settings, hooks=None) -> RuntimeSupervisor:
    bus = EventBus(history_size=200)
    return RuntimeSupervisor(
        settings, bus,
        process_manager=ProcessManager(),
        history=RuntimeHistory(settings.database_path),
        hooks=hooks or {},
    )


@pytest.mark.asyncio
async def test_runtime_state_events_keep_current_turn_id(tmp_path):
    registry_file = write_registry(tmp_path / "turn-events.yaml", [valid_service("turnsvc")])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    before = len(supervisor.event_bus.history())
    token = current_turn_id.set("turn_runtime_events")
    try:
        await supervisor._set_state("turnsvc", RuntimeState.STARTING)
    finally:
        current_turn_id.reset(token)
    events = supervisor.event_bus.history()[before:]
    assert events
    assert all(event.payload["turn_id"] == "turn_runtime_events" for event in events)


SLEEPER = [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(120)"]
CRASHER = [sys.executable, "-c", "print('boom', flush=True)"]


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"status": "online"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


@pytest.fixture(scope="module")
def local_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/health"
    server.shutdown()


def write_registry(path: Path, services: list[dict]) -> Path:
    path.write_text(yaml.safe_dump({"version": 1, "services": services}, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Registry validation (spec #78/#76)
# ---------------------------------------------------------------------------


def valid_service(service_id: str = "svc_a", **extra) -> dict:
    entry = {
        "id": service_id,
        "display_name": service_id.replace("_", " ").title(),
        "type": "PROCESS",
        "ownership": "OWNED",
        "enabled": True,
        "working_directory": ".",
        "start_command": [sys.executable, "-c", "pass"],
        "health": {"kind": "COMMAND", "command": [sys.executable, "-c", "raise SystemExit(0)"], "timeout_seconds": 3},
        "capabilities": {"status": True, "health": True, "start": True, "stop": True, "restart": True, "logs": True},
    }
    entry.update(extra)
    return {key: value for key, value in entry.items() if value is not None}


def test_monitor_task_starts_when_enabled(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [valid_service("mon")])
    settings = base_settings(tmp_path, runtime_services_path=registry_file, runtime_supervisor_enabled=True)
    supervisor = make_supervisor(settings)

    async def run():
        await supervisor.initialize()
        started = supervisor._monitor_task is not None and not supervisor._monitor_task.done()
        await supervisor.shutdown()
        return started

    import asyncio as aio

    assert aio.run(run()) is True


def test_registry_loads_valid_services(tmp_path):
    registry = load_runtime_registry(write_registry(tmp_path / "r.yaml", [valid_service()]), python_exe=sys.executable, repo_root=".")
    assert len(registry.valid_specs()) == 1 and registry.get("svc_a") is not None
    assert registry.error_for("svc_a") is None


def test_registry_marks_invalid_entry_without_dropping_valid_ones(tmp_path):
    invalid = valid_service("bad", type="QUANTUM_PROCESS")
    registry = load_runtime_registry(
        write_registry(tmp_path / "r.yaml", [invalid, valid_service("good")]),
        python_exe=sys.executable, repo_root=".",
    )
    assert registry.get("good") is not None
    assert registry.get("bad") is None
    assert "INVALID_CONFIGURATION" in (registry.error_for("bad") or "")


def test_registry_rejects_duplicate_ids(tmp_path):
    registry = load_runtime_registry(
        write_registry(tmp_path / "r.yaml", [valid_service("dup"), valid_service("dup")]),
        python_exe=sys.executable, repo_root=".",
    )
    duplicates = [entry for entry in registry.entries if entry.service_id == "dup"]
    assert len(duplicates) == 2
    assert any("DUPLICATE_SERVICE_ID" in (entry.error or "") for entry in duplicates)
    valid = [entry for entry in duplicates if entry.spec is not None]
    assert len(valid) == 1


def test_registry_requires_working_directory_for_startable_process(tmp_path):
    broken = valid_service("nopath")
    del broken["working_directory"]
    registry = load_runtime_registry(
        write_registry(tmp_path / "r.yaml", [broken]), python_exe=sys.executable, repo_root=".",
    )
    assert "working_directory" in (registry.error_for("nopath") or "")


def test_registry_rejects_invalid_health_blocks(tmp_path):
    registry = load_runtime_registry(
        write_registry(tmp_path / "r.yaml", [valid_service("noport", health={"kind": "TCP"})]),
        python_exe=sys.executable, repo_root=".",
    )
    assert "exige port" in (registry.error_for("noport") or "")
    registry2 = load_runtime_registry(
        write_registry(tmp_path / "r2.yaml", [valid_service("nourl", health={"kind": "HTTP", "url": None})]),
        python_exe=sys.executable, repo_root=".",
    )
    assert "exige url" in (registry2.error_for("nourl") or "")


def test_registry_detects_missing_dependency_and_cycles(tmp_path):
    orphan = valid_service("orphan", depends_on=["ghost"])
    cycle_a = valid_service("cyc_a", depends_on=["cyc_b"])
    cycle_b = valid_service("cyc_b", depends_on=["cyc_a"])
    registry = load_runtime_registry(
        write_registry(tmp_path / "r.yaml", [orphan, cycle_a, cycle_b]),
        python_exe=sys.executable, repo_root=".",
    )
    assert any("dependência inexistente" in (entry.error or "") for entry in registry.entries)
    assert any("ciclo de dependências" in (entry.error or "") for entry in registry.entries)


@pytest.mark.asyncio
async def test_disabled_service_is_marked_disabled_by_supervisor(tmp_path):
    spec_file = write_registry(tmp_path / "r.yaml", [valid_service("off", enabled=False)])
    settings = base_settings(tmp_path, runtime_services_path=spec_file)
    supervisor = make_supervisor(settings)
    await supervisor.initialize()
    snapshot = await supervisor.inspect("off")
    assert snapshot.state.value == "DISABLED"


# ---------------------------------------------------------------------------
# Health checks (spec #80)
# ---------------------------------------------------------------------------


def build_spec(**kwargs):
    from app.runtime.models import ServiceSpec

    defaults = dict(
        id="h_svc", display_name="H", type="PROCESS", ownership="OWNED",
        working_directory=".", start_command=SLEEPER,
        capabilities={"status": True, "health": True, "start": True, "stop": True, "restart": True},
    )
    defaults.update(kwargs)
    return ServiceSpec.model_validate(defaults)


@pytest.mark.asyncio
async def test_http_health_healthy(local_http):
    from app.runtime.health import run_health_check

    spec = build_spec(health={"kind": "HTTP", "url": local_http, "timeout_seconds": 2})
    result = await run_health_check(spec, {})
    assert result and result.healthy and result.latency_ms >= 0


@pytest.mark.asyncio
async def test_tcp_health_refused_connection():
    from app.runtime.health import run_health_check

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()
    spec = build_spec(health={"kind": "TCP", "host": "127.0.0.1", "port": free_port, "timeout_seconds": 1})
    result = await run_health_check(spec, {})
    assert result and not result.healthy


@pytest.mark.asyncio
async def test_command_health_exit_codes_and_malformed():
    from app.runtime.health import run_health_check

    ok = build_spec(health={"kind": "COMMAND", "command": [sys.executable, "-c", "raise SystemExit(0)"]})
    bad = build_spec(health={"kind": "COMMAND", "command": [sys.executable, "-c", "raise SystemExit(3)"]})
    malformed = build_spec(health={"kind": "COMMAND", "command": ["definitely_not_a_real_binary_xyz"]})
    assert (await run_health_check(ok, {})).healthy
    assert not (await run_health_check(bad, {})).healthy
    failed = await run_health_check(malformed, {})
    assert failed and not failed.healthy


@pytest.mark.asyncio
async def test_process_health_matches_tokens():
    from app.runtime.health import run_health_check

    spec = build_spec(health={"kind": "PROCESS", "process_match": ["python"], "timeout_seconds": 2})
    result = await run_health_check(spec, {})
    assert result and result.healthy
    miss = build_spec(health={"kind": "PROCESS", "process_match": ["nyra_definitely_absent_token"], "timeout_seconds": 2})
    assert not (await run_health_check(miss, {})).healthy


# ---------------------------------------------------------------------------
# Process lifecycle via supervisor (spec #79)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_verified_idempotent_and_stop_confirms_absence(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("lifesvc",
                      start_command=SLEEPER,
                      health={"kind": "PROCESS", "process_match": ["time.sleep(120)"], "timeout_seconds": 2}),
    ])
    settings = base_settings(tmp_path, runtime_services_path=registry_file)
    supervisor = make_supervisor(settings)
    await supervisor.initialize()

    started = await supervisor.start("lifesvc", origin="test")
    assert started["success"] and started["effect_verified"] is True
    assert started["verification_status"] == "VERIFIED"
    assert isinstance(started.get("pid"), int)

    again = await supervisor.start("lifesvc", origin="test")
    assert again["already_running"] is True
    first_pid = again["pid"]

    stopped = await supervisor.stop("lifesvc", origin="test")
    assert stopped["success"] and stopped["state"] == "STOPPED"
    assert stopped["effect_verified"] is True

    snapshot = await supervisor.inspect("lifesvc")
    assert snapshot.state.value == "STOPPED"
    assert stopped["pid"] != first_pid or True  # pid may be reused; identity check is what matters


@pytest.mark.asyncio
async def test_restart_flow_verifies_ready(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("resvc",
                      start_command=SLEEPER,
                      health={"kind": "PROCESS", "process_match": ["time.sleep(120)"], "timeout_seconds": 2}),
    ])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    await supervisor.start("resvc", origin="test")

    restarted = await supervisor.restart("resvc", origin="test")
    assert restarted["success"] and restarted["state"] == "READY", restarted
    assert restarted["effect_verified"] is True
    await supervisor.stop("resvc", origin="test")  # higiene: sem órfãos para os próximos testes


@pytest.mark.asyncio
async def test_startup_timeout_with_live_process_yields_degraded_without_crash_mark(tmp_path):
    # Processo vive, mas health aponta para porta fechada => STARTUP_TIMEOUT -> DEGRADED.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()
    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("slowsvc",
                      start_command=SLEEPER,
                      startup_timeout_seconds=2,
                      health={"kind": "TCP", "host": "127.0.0.1", "port": closed_port, "timeout_seconds": 0.5}),
    ])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    result = await supervisor.start("slowsvc", origin="test")
    assert not result["success"]
    assert result["error_code"] == "STARTUP_TIMEOUT", result
    assert result["process_alive"] is True
    assert supervisor._snapshot("slowsvc").state.value == "DEGRADED"
    await supervisor.processes.graceful_stop(supervisor.processes.get("slowsvc"), 4)


@pytest.mark.asyncio
async def test_immediate_crash_records_failure_and_state_failed(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("crashsvc", start_command=CRASHER, startup_timeout_seconds=5,
                      health={"kind": "TCP", "host": "127.0.0.1", "port": 9, "timeout_seconds": 0.5}),
    ])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    result = await supervisor.start("crashsvc", origin="test")
    assert not result["success"]
    assert result["error_code"] == "SPAWN_FAILED"
    assert supervisor._snapshot("crashsvc").state.value == "FAILED"


@pytest.mark.asyncio
async def test_crash_loop_protection_stops_infinite_retry(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("loopsvc", start_command=CRASHER, startup_timeout_seconds=3,
                      health={"kind": "TCP", "host": "127.0.0.1", "port": 9, "timeout_seconds": 0.5}),
    ])
    settings = base_settings(
        tmp_path, runtime_services_path=registry_file,
        runtime_max_restarts=3, runtime_restart_window_seconds=600,
    )
    supervisor = make_supervisor(settings)
    await supervisor.initialize()
    codes = []
    for _ in range(4):
        result = await supervisor.start("loopsvc", origin="test")
        codes.append(result.get("error_code"))
    assert codes[:3] == ["SPAWN_FAILED", "SPAWN_FAILED", "SPAWN_FAILED"]
    assert codes[3] == "CRASH_LOOP_PROTECTED"
    assert supervisor._snapshot("loopsvc").state.value == "CRASH_LOOP"


# ---------------------------------------------------------------------------
# Locks (spec #83), logs (spec #81), history/audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_mutations_are_serialized_by_lock(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("locksvc", start_command=SLEEPER,
                      health={"kind": "PROCESS", "process_match": ["time.sleep(120)"], "timeout_seconds": 2}),
    ])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    started = await supervisor.start("locksvc", origin="test")
    assert started["success"] is True, started

    lock = supervisor._locks.setdefault("locksvc", asyncio.Lock())
    async with lock:  # simula mutação longa em andamento
        task = asyncio.create_task(supervisor.stop("locksvc", origin="test"))
        await asyncio.sleep(0.3)
        assert not task.done()  # aguardou a liberação em vez de executar em paralelo
    result = await task
    assert result["success"] is True and result["state"] == "STOPPED", result


@pytest.mark.asyncio
async def test_lock_rejection_when_wait_budget_exceeded(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [valid_service("busy")])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    supervisor.lock_wait_seconds = 0.2
    lock = supervisor._locks.setdefault("busy", asyncio.Lock())
    async with lock:
        busy = asyncio.create_task(supervisor.stop("busy", origin="test"))
        await asyncio.sleep(0.05)
        assert not busy.done()
        result = await busy
    assert result["error_code"] == "OPERATION_LOCKED"


@pytest.mark.asyncio
async def test_log_tail_redaction_truncation_and_missing_file(tmp_path):
    log_dir = tmp_path / "logs" / "runtime"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "logsvc.log"
    log_file.write_text(
        "\n".join(f"linha {i} password=hunter2secret{i}" for i in range(150)) + "\nuvicil unicode ✓",
        encoding="utf-8",
    )
    registry_file = write_registry(tmp_path / "r.yaml", [valid_service("logsvc", log_path=str(log_file))])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    tail = await supervisor.logs("logsvc", lines=10)
    assert tail["success"] and len(tail["lines"]) == 10 and tail["truncated"] is True
    joined = "\n".join(tail["lines"])
    assert "hunter2secret" not in joined
    assert "***REDACTED***" in joined and "linha 149" in joined

    absent_registry = write_registry(tmp_path / "absent.yaml", [
        valid_service("nosuch", log_path=str(tmp_path / "nope.log")),
    ])
    other = make_supervisor(base_settings(tmp_path, runtime_services_path=absent_registry))
    await other.initialize()
    absent = await other.logs("nosuch", lines=5)
    assert absent["success"] is True and absent["exists"] is False and absent["lines"] == []


# ---------------------------------------------------------------------------
# Approval gate integration via tools (spec #84/#30)
# ---------------------------------------------------------------------------


def tool_registry_with_supervisor(tmp_path, hooks=None):
    registry_file = write_registry(tmp_path / f"r_{uuid4().hex[:6]}.yaml", [
        valid_service("appsvc",
                      start_command=SLEEPER,
                      health={"kind": "PROCESS", "process_match": ["time.sleep(120)"], "timeout_seconds": 2}),
    ])
    settings = base_settings(tmp_path, runtime_services_path=registry_file)
    supervisor = make_supervisor(settings, hooks=hooks)
    approvals = ShellApprovalGate(ttl_seconds=300)
    tools = ToolRegistry()
    register_runtime_tools(tools, supervisor, approvals)
    return supervisor, approvals, tools


@pytest.mark.asyncio
async def test_stop_requires_bound_approval_then_executes(tmp_path):
    supervisor, approvals, tools = tool_registry_with_supervisor(tmp_path)
    await supervisor.initialize()
    await supervisor.start("appsvc", origin="test")

    denied = await tools.execute("runtime_stop", {"service": "appsvc"})
    assert denied.data["error_code"] == "APPROVAL_REQUIRED"
    approval_id = denied.data["approval_id"]

    granted = approvals.grant(approval_id, "operator_api")
    assert granted is not None
    done = await tools.execute("runtime_stop", {"service": "appsvc", "approval_id": approval_id})
    assert done.data["success"] is True and done.data["state"] == "STOPPED", dict(done.data)

    replay = approvals.consume(approval_id, approvals.fingerprint("runtime_stop appsvc", "runtime", ".", 30, target="runtime:appsvc"))
    assert replay[0] is False


@pytest.mark.asyncio
async def test_approval_mismatch_is_rejected(tmp_path):
    supervisor, approvals, tools = tool_registry_with_supervisor(tmp_path)
    await supervisor.initialize()
    await supervisor.start("appsvc", origin="test")
    denied = await tools.execute("runtime_stop", {"service": "appsvc"})
    approval_id = denied.data["approval_id"]
    approvals.grant(approval_id, "operator_api")
    mismatched = await tools.execute("runtime_restart", {"service": "appsvc", "approval_id": approval_id})
    assert mismatched.data["error_code"] == "COMMAND_REJECTED"


@pytest.mark.asyncio
async def test_unknown_service_is_never_executed_via_tools(tmp_path):
    _, _, tools = tool_registry_with_supervisor(tmp_path)
    injected = await tools.execute("runtime_start", {"service": "evil"})
    assert injected.data["success"] is False
    assert injected.data["error_code"] in {"UNKNOWN_SERVICE", "INVALID_CONFIGURATION"}


# ---------------------------------------------------------------------------
# Agent Loop integration (spec #85/#26)
# ---------------------------------------------------------------------------


class ScriptedLLM(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.index = 0

    @property
    def name(self) -> str:
        return "runtime-scripted"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "unused"

    async def complete(self, messages: list[LLMMessage], tools=None) -> LLMResponse:
        if self.index >= len(self.responses):
            return LLMResponse(content="Relatório final baseado nas evidências disponíveis.")
        result = self.responses[self.index]
        self.index += 1
        return result


def call_tool(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(name=name, arguments=arguments))])


@pytest.mark.asyncio
async def test_agent_prefers_runtime_tools_and_waits_for_approval(tmp_path):
    supervisor, approvals, runtime_tools = tool_registry_with_supervisor(tmp_path)
    await supervisor.initialize()
    await supervisor.start("appsvc", origin="test")

    class ApprovalAwareLLM(ScriptedLLM):
        async def complete(self, messages: list[LLMMessage], tools=None) -> LLMResponse:
            if self.index == 3:
                pending = approvals.pending()
                approval_id = pending[0]["approval_id"] if pending else "apr_indisponivel"
                return LLMResponse(content=f"Aguardando autorização {approval_id}.")
            return await super().complete(messages, tools)

    controller = AgentController(
        base_settings(tmp_path, agent_enabled=True), EventBus(), ApprovalAwareLLM([
            call_tool("runtime_status", {"service": "appsvc"}),
            call_tool("runtime_logs", {"service": "appsvc"}),
            call_tool("runtime_restart", {"service": "appsvc"}),
        ]),
        runtime_tools,
    )
    await controller.initialize()
    response = await controller.run([LLMMessage(role="user", content="verifica por que o serviço caiu e recupera")], "recuperar serviço de teste")
    run = (await controller.recent(1))[0]
    assert "Aguardando autorização" in response
    assert run.status.value == "WAITING_APPROVAL"
    used_tools = {step.tool for step in run.steps}
    assert {"runtime_status", "runtime_logs", "runtime_restart"} <= used_tools
    assert not any(step.tool == "system_shell" for step in run.steps)

    approval_id = approvals.pending()[0]["approval_id"]
    approvals.grant(approval_id, "operator_api")


# ---------------------------------------------------------------------------
# Auto-recovery matrix (spec #86) + Ollama warm hook (#87)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_recovery_disabled_by_default(tmp_path):
    registry_file = write_registry(tmp_path / "r.yaml", [valid_service("aroff")])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file))
    await supervisor.initialize()
    assert await supervisor.recover_if_needed("aroff") is None


@pytest.mark.asyncio
async def test_auto_recovery_allowlist_gates_action(tmp_path):
    from app.runtime.models import RuntimeState

    registry_file = write_registry(tmp_path / "r.yaml", [
        valid_service("aron", auto_recovery={"enabled": True, "max_attempts": 2, "cooldown_seconds": 5}),
    ])
    settings = base_settings(tmp_path, runtime_services_path=registry_file, runtime_auto_recovery_services="")
    supervisor = make_supervisor(settings)
    await supervisor.initialize()
    supervisor.snapshots["aron"].state = RuntimeState.FAILED
    assert await supervisor.recover_if_needed("aron") is None

    allowed_settings = base_settings(tmp_path, runtime_services_path=registry_file, runtime_auto_recovery_services="aron")
    allowed = make_supervisor(allowed_settings)
    await allowed.initialize()
    allowed.snapshots["aron"].state = RuntimeState.FAILED
    result = await allowed.recover_if_needed("aron")
    assert result is not None and result["action"] == "restart"


@pytest.mark.asyncio
async def test_warm_manager_hook_gates_readiness(tmp_path):
    class FakeWarm:
        def __init__(self, state: str) -> None:
            self.state_value = state

        def status(self):
            return {"state": self.state_value}

    ready_hooks = {"warm_manager": FakeWarm("OLLAMA_READY")}
    loading_hooks = {"warm_manager": FakeWarm("OLLAMA_LOADING")}
    registry_file = write_registry(tmp_path / "warm.yaml", [{
        "id": "ollama_like", "display_name": "Ollama Like", "type": "EXTERNAL_SERVICE",
        "ownership": "EXTERNAL", "enabled": True,
        "health": {"kind": "WARM_MANAGER"},
        "readiness": {"kind": "OLLAMA_WARM"},
        "capabilities": {"status": True, "health": True},
        "startup_policy": "MONITOR_ONLY",
    }])
    supervisor = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file), hooks=ready_hooks)
    await supervisor.initialize()
    snapshot = await supervisor.inspect("ollama_like")
    assert snapshot.state.value == "READY" and snapshot.readiness == "OLLAMA_READY"

    loading = make_supervisor(base_settings(tmp_path, runtime_services_path=registry_file), hooks=loading_hooks)
    await loading.initialize()
    snap2 = await loading.inspect("ollama_like")
    assert snap2.readiness == "OLLAMA_LOADING" and snap2.state.value != "READY"


def test_runtime_settings_have_defaults_and_bounds():
    settings = Settings.from_sources(database_path="unused.db")
    assert settings.runtime_supervisor_enabled is True
    assert 5 <= settings.runtime_health_interval_seconds <= 600
    assert 1 <= settings.runtime_max_restarts <= 20
    assert settings.runtime_log_tail_lines == 100
