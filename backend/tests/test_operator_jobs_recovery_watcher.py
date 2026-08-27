"""Persistent Jobs + Recovery + Watcher tests (spec Partes F/H/I, V/X/Y)."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from app.operator.jobs import PersistentJobManager
from app.operator.recovery import RecoveryEngine, RecoveryState


# ------------------------------------------------------------------ Parte F/V
def _sleeper(seconds: int = 30) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


@pytest.mark.asyncio
async def test_job_start_status_and_progress(tmp_path):
    manager = PersistentJobManager(database_path=tmp_path / "test.db")
    await manager.initialize()
    job = await manager.start("sleeper-teste", [sys.executable, "-c",
                              "import time\nfor i in range(10):\n"
                              "    print(f'{i*10}% progresso', flush=True)\n"
                              "    time.sleep(0.3)\n"])
    assert job["success"] is True
    job_id = job["job"]["job_id"]
    assert job["job"]["state"] == "RUNNING"
    try:
        await asyncio.sleep(2.5)
        status = await manager.status(job_id)
        assert status["success"] is True
        assert status["job"]["progress"] is not None  # §127 extraído do output real
        assert 0 <= float(status["job"]["progress"]) <= 100
        logs = await manager.logs(job_id)
        assert logs["success"] is True and "%" in logs["stdout_tail"]
    finally:
        await manager.cancel(job_id)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_job_cancel_cleans_process(tmp_path):
    manager = PersistentJobManager(database_path=tmp_path / "test.db")
    await manager.initialize()
    job = await manager.start("sleeper-cancel", _sleeper(60))
    job_id = job["job"]["job_id"]
    pid = int(job["job"]["pid"])

    cancelled = await manager.cancel(job_id)
    assert cancelled["success"] is True
    assert cancelled["killed_process"] is True
    time.sleep(0.6)

    from app.operator.jobs import _identity_alive

    assert not _identity_alive(pid, None) or _identity_alive(pid, job["job"].get("create_time")) is False
    after = await manager.status(job_id)
    assert after["job"]["state"] == "CANCELLED"  # §122/§290 cleanup validado
    await manager.shutdown()


@pytest.mark.asyncio
async def test_job_reattach_marks_orphan_failed(tmp_path):
    """§132/§134: registro RUNNING cujo processo sumiu vira FAILED no reattach."""
    manager = PersistentJobManager(database_path=tmp_path / "test.db")
    await manager.initialize()
    fake_pid = 4_000_000 + (int(time.time()) % 1000)  # pid improvável de existir
    await manager._save({
        "job_id": "job_deadbeef001", "name": "orfa", "type": "process",
        "state": "RUNNING", "started_at": time.time(), "finished_at": None,
        "exit_code": None, "pid": fake_pid, "create_time": time.time() - 5,
        "resource_key": "job:orfa", "timeout_seconds": None,
        "command_preview": "teste",
    })
    outcome = await manager.reattach()
    assert outcome["success"] is True and outcome["orphaned"] >= 1
    record = await manager._load("job_deadbeef001")
    assert record["state"] in {"FAILED", "UNKNOWN"}
    await manager.shutdown()


# ------------------------------------------------------------------ Parte H/X
@pytest.mark.asyncio
async def test_recovery_file_backup_and_rollback(tmp_path):
    from app.tools.shell_approval import ShellApprovalGate

    gate = ShellApprovalGate()
    engine = RecoveryEngine(approvals=gate, database_path=tmp_path / "rec.db")
    await engine.initialize()
    target = tmp_path / "config-teste.yaml"  # §296: nunca config real
    target.write_text("valor: original\n", encoding="utf-8")

    prepared = await engine.prepare_file_backup(str(target), action="teste rollback")
    assert prepared["success"] is True
    transaction_id = prepared["transaction_id"]

    target.write_text("valor: quebrado\n", encoding="utf-8")
    engine.mark_written(transaction_id, None or __import__("hashlib").sha256(
        target.read_bytes()).hexdigest())

    pending = await engine.rollback(transaction_id)
    gate.grant(pending["approval_id"], "test")
    rolled = await engine.rollback(transaction_id, approval_id=pending["approval_id"])
    assert rolled["success"] is True, rolled
    assert rolled["state"] == "RECOVERED"
    assert target.read_text(encoding="utf-8") == "valor: original\n"  # §166 verify


@pytest.mark.asyncio
async def test_recovery_refuses_blind_rollback_after_user_edit(tmp_path):
    """§167: mudança do usuário depois do snapshot bloqueia rollback cego."""
    from app.tools.shell_approval import ShellApprovalGate

    gate = ShellApprovalGate()
    engine = RecoveryEngine(approvals=gate, database_path=tmp_path / "rec.db")
    await engine.initialize()
    target = tmp_path / "cfg.txt"
    target.write_text("original", encoding="utf-8")
    prepared = await engine.prepare_file_backup(str(target))
    transaction_id = prepared["transaction_id"]
    # Nossa ação escreveu...
    target.write_text("escrito-pela-acao", encoding="utf-8")
    engine.mark_written(transaction_id, __import__("hashlib").sha256(b"escrito-pela-acao").hexdigest())
    # ...mas o USUÁRIO alterou depois.
    target.write_text("edicao-do-usuario", encoding="utf-8")
    pending = await engine.rollback(transaction_id, auto=True)
    assert pending["auto_rollback_blocked"] is True
    gate.grant(pending["approval_id"], "test")
    rolled = await engine.rollback(transaction_id, approval_id=pending["approval_id"])
    assert rolled["success"] is False
    assert rolled["state"] == RecoveryState.RECOVERY_REQUIRED.value
    assert target.read_text(encoding="utf-8") == "edicao-do-usuario"


@pytest.mark.asyncio
async def test_recovery_consumes_exact_one_use_approval(tmp_path):
    from app.tools.shell_approval import ShellApprovalGate

    gate = ShellApprovalGate()
    engine = RecoveryEngine(approvals=gate, database_path=tmp_path / "approved-rec.db")
    await engine.initialize()
    target = tmp_path / "approved.txt"
    target.write_text("original", encoding="utf-8")
    prepared = await engine.prepare_file_backup(str(target), action="rollback aprovado")
    target.write_text("changed", encoding="utf-8")
    engine.mark_written(
        prepared["transaction_id"],
        __import__("hashlib").sha256(b"changed").hexdigest(),
    )

    pending = await engine.rollback(prepared["transaction_id"])
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    other = tmp_path / "other-approved.txt"
    other.write_text("other-original", encoding="utf-8")
    other_prepared = await engine.prepare_file_backup(
        str(other), action="rollback aprovado",
    )
    other.write_text("other-changed", encoding="utf-8")
    engine.mark_written(
        other_prepared["transaction_id"],
        __import__("hashlib").sha256(b"other-changed").hexdigest(),
    )
    gate.grant(pending["approval_id"], "test")
    wrong = await engine.rollback(
        other_prepared["transaction_id"], approval_id=pending["approval_id"],
    )
    assert wrong["error_code"] == "APPROVAL_INVALID"
    rolled = await engine.rollback(
        prepared["transaction_id"], approval_id=pending["approval_id"],
    )
    assert rolled["success"] is True
    assert target.read_text(encoding="utf-8") == "original"


# ------------------------------------------------------------------ Parte I/Y
@pytest.mark.asyncio
async def test_watcher_detects_created_file(tmp_path):
    from app.operator.watcher import DesktopWatcher

    watcher = DesktopWatcher(None)
    await watcher.start()
    watch_dir = tmp_path / "observado"
    watch_dir.mkdir()
    registration = await watcher.register(["file.created"], filters={"path": str(watch_dir)},
                                          ttl_seconds=120)
    assert registration["success"] is True
    watch_id = registration["watch_id"]
    try:
        (watch_dir / "novo-arquivo.txt").write_text("conteudo", encoding="utf-8")
        deadline = time.time() + 12
        events: list[dict] = []
        while time.time() < deadline:
            result = watcher.events(watch_id)
            events = result.get("events") or []
            if any(item["type"] == "file.created" for item in events):
                break
            await asyncio.sleep(0.7)
        assert any(item["type"] == "file.created" for item in events), events[:5]
    finally:
        await watcher.cancel(watch_id)
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_detects_process_exit(tmp_path):
    from app.operator.watcher import DesktopWatcher

    watcher = DesktopWatcher(None)
    await watcher.start()
    registration = await watcher.register(
        ["process.started", "process.exited"],
        filters={"process": sys.executable.rsplit("\\", 1)[-1]},
        ttl_seconds=120,
    )
    assert registration["success"] is True
    watch_id = registration["watch_id"]
    try:
        import subprocess

        process = subprocess.Popen(  # noqa: S603 - processo teste curto (vida > intervalo do watcher)
            [sys.executable, "-c", "import time; time.sleep(3)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.time() + 18
        types_seen: set[str] = set()
        while time.time() < deadline:
            result = watcher.events(watch_id)
            types_seen |= {item["type"] for item in (result.get("events") or [])}
            if "process.exited" in types_seen:
                break
            await asyncio.sleep(0.7)
        process.wait(timeout=10)
        assert "process.exited" in types_seen  # §298
    finally:
        await watcher.cancel(watch_id)
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_ttl_expires_and_limit(tmp_path):
    from app.operator.watcher import DesktopWatcher

    watcher = DesktopWatcher(None, default_ttl_seconds=15, max_active=2)
    await watcher.start()
    first = await watcher.register(["window.focused"], ttl_seconds=15)
    second = await watcher.register(["service.changed"], ttl_seconds=15)
    third = await watcher.register(["file.created"], filters={"path": str(tmp_path)})
    assert first["success"] is True and second["success"] is True
    assert third["success"] is False and third["error_code"] == "WATCH_LIMIT"  # §181
    watcher._watches[first["watch_id"]].expires_at = time.time() - 1  # força TTL vencido
    listing = watcher.list_watches()
    ids = {item["watch_id"] for item in listing["watches"]}
    assert first["watch_id"] not in ids  # TTL expirou (§180)
    await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_device_events_honest_unavailable():
    from app.operator.watcher import DesktopWatcher

    watcher = DesktopWatcher(None)
    outcome = await watcher.register(["device.connected"])
    assert outcome["success"] is False
    assert outcome["error_code"] == "CAPABILITY_UNAVAILABLE"
