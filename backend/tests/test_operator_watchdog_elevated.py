"""Watchdog + Elevated Session + Browser V2 tests (spec Partes L/E/T)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.operator.contexts import OperatorContextRegistry, ContextKind


# ------------------------------------------------------------------ Parte L
@pytest.mark.asyncio
async def test_watchdog_bridge_writes_request_and_cooldowns(tmp_path):
    from app.operator.watchdog_bridge import WatchdogBridge

    bridge = WatchdogBridge(requests_dir=tmp_path / "requests", min_interval_seconds=0.0)
    from app.events import Event, EventType

    event = Event(type=EventType.RUNTIME_FAILED,
                  payload={"service_id": "kazumi_backend", "reason": "health failed 3x"})
    await bridge.handle_event(event)
    files = list((tmp_path / "requests").glob("*.json"))
    assert len(files) == 1  # §227 canal de request one-shot
    document = json.loads(files[0].read_text(encoding="utf-8"))
    assert document["action"] == "restart_backend"

    # eventos irrelevantes não geram request
    other = Event(type=EventType.RUNTIME_HEALTH_PASSED, payload={"service_id": "ollama"})
    await bridge.handle_event(other)
    assert len(list((tmp_path / "requests").glob("*.json"))) == 1


def test_watchdog_component_guard_crash_loop():
    """§219/§220/§221 sem subprocess: lógica pura do guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "kazumi_watchdog_mod", Path(__file__).resolve().parents[2] / "watchdog" / "kazumi_watchdog.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 1) Threshold de falhas consecutivas -> RESTART.
    guard = module.ComponentGuard("backend")
    decision = None
    for _ in range(module.FAILURE_THRESHOLD):
        decision = guard.record(False)
    assert decision == "RESTART"  # §219
    guard.mark_restart()
    assert guard.consecutive_failures == 0 and len(guard.restarts) == 1

    # 2) Janela cheia de restarts -> CRASH_LOOP_PROTECTED (sem retries infinitos).
    guard.restarts = [time.time()] * module.RESTART_LIMIT
    blocked = None
    for _ in range(module.FAILURE_THRESHOLD):
        blocked = guard.record(False)
        if blocked == "CRASH_LOOP_PROTECTED":
            break
    assert blocked == "CRASH_LOOP_PROTECTED"

    # 3) Sucesso zera contador de falhas.
    healthy = module.ComponentGuard("frontend")
    healthy.record(False)
    assert healthy.record(True) is None


# ------------------------------------------------------------------ Parte E
@pytest.mark.asyncio
async def test_elevated_session_requires_approval_and_fails_closed():
    from app.operator.elevated_sessions import ElevatedSessionManager

    manager = ElevatedSessionManager(approvals=None)
    opened = await manager.open(reason="teste unitário", ttl_seconds=60)
    assert opened["success"] is False  # sem gate → fail-closed (não abre UAC em teste)
    assert opened["error_code"] in {"APPROVAL_REQUIRED"}

    executed = await manager.execute("sessao-inexistente", "Get-Date")
    assert executed["success"] is False
    assert executed["error_code"] == "SESSION_NOT_FOUND"
    status = manager.status()
    assert status["active_sessions"] == []


@pytest.mark.asyncio
async def test_elevated_execute_blocks_destructive_without_own_approval():
    """§112: mesmo dentro de uma sessão, DESTRUCTIVE exige approval próprio."""
    from app.operator.elevated_sessions import ElevatedSessionManager

    manager = ElevatedSessionManager(approvals=None)
    session = object.__new__(type(manager).__mro__[0].__bases__ and __import__("app.operator.elevated_sessions", fromlist=["ElevatedSession"]).ElevatedSession)
    # Sessão sintética válida (sem host): só para chegar ao classificador.
    import time as _time

    session.session_id = "esess_fake0001"
    session.user = "tester"
    session.started_at = _time.time()
    session.expires_at = _time.time() + 60
    session.capabilities = ["shell:powershell"]
    session.pipe_name = "kazumi-elevated-fake"
    session.token = "x"
    session.host_pid = None
    manager._sessions[session.session_id] = session

    outcome = await manager.execute(
        "esess_fake0001",
        "Remove-Item C:\\importante -Recurse -Force",
        shell="powershell",
        approval_id=None,
    )
    assert outcome["success"] is False
    assert outcome["error_code"] == "APPROVAL_REQUIRED"  # §112 fail-closed


def test_elevated_approval_helper_consumes_exact_fingerprint():
    from app.operator.elevated_sessions import ElevatedSessionManager
    from app.tools.shell_approval import ShellApprovalGate

    gate = ShellApprovalGate()
    manager = ElevatedSessionManager(approvals=gate)
    description = "Abrir sessão administrativa de teste"
    pending = manager._require_approval(
        description=description, risk="ELEVATED", approval_id=None,
    )
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    gate.grant(pending["approval_id"], "test")
    assert manager._require_approval(
        description=description, risk="ELEVATED",
        approval_id=pending["approval_id"],
    ) is None


@pytest.mark.asyncio
async def test_elevated_destructive_approval_binds_full_command_shell_and_timeout():
    from app.operator.elevated_sessions import ElevatedSession, ElevatedSessionManager
    from app.tools.shell_approval import ShellApprovalGate

    gate = ShellApprovalGate()
    manager = ElevatedSessionManager(approvals=gate)
    now = __import__("time").time()
    session = ElevatedSession(
        session_id="esess_binding001", user="tester", started_at=now,
        expires_at=now + 60, capabilities=["shell:powershell"],
        pipe_name="kazumi-elevated-binding", token="x", host_pid=None,
    )
    manager._sessions[session.session_id] = session
    manager._send_request = lambda *_args, **_kwargs: {
        "_transport_ok": True, "ok": True, "exit_code": 0,
        "stdout": "", "stderr": "", "timed_out": False, "duration_ms": 1,
    }
    command = "Remove-Item C:\\important.txt -Force # " + ("x" * 220)
    pending = await manager.execute(
        session.session_id, command, shell="powershell", timeout_seconds=60,
    )
    gate.grant(pending["approval_id"], "test")
    changed = await manager.execute(
        session.session_id, command + "; Remove-Item C:\\other.txt -Force",
        shell="powershell", timeout_seconds=60, approval_id=pending["approval_id"],
    )
    assert changed["error_code"] == "APPROVAL_INVALID"
    changed_shell = await manager.execute(
        session.session_id, command, shell="cmd", timeout_seconds=60,
        approval_id=pending["approval_id"],
    )
    assert changed_shell["error_code"] == "APPROVAL_INVALID"
    exact = await manager.execute(
        session.session_id, command, shell="powershell", timeout_seconds=60,
        approval_id=pending["approval_id"],
    )
    assert exact["success"] is True

