"""Local Operator unit tests: filesystem ACT→VERIFY, approvals, guards, power."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.desktop.operator import OperatorController
from app.events import EventBus
from app.tools.shell_approval import ShellApprovalGate


def make_controller(tmp_path: Path) -> tuple[OperatorController, ShellApprovalGate]:
    gate = ShellApprovalGate()
    return OperatorController(EventBus(), gate), gate


async def grant_for(controller: OperatorController, gate: ShellApprovalGate, action: str, target: str,
                    timeout_seconds: int = 120, binding_digest: str = "") -> str:
    """Create + grant an approval whose fingerprint matches controller._approval."""
    import os

    record = gate.request(
        command=f"{action}:{target}:{binding_digest or 'none'}",
        shell="local_operator", working_directory=os.getcwd(),
        timeout_seconds=timeout_seconds, risk_level="LOW_RISK", target="local", agent_run_id=None,
    )
    assert gate.grant(record.approval_id, "test") is not None
    return record.approval_id


@pytest.mark.asyncio
async def test_fs_write_mkdir_read_rename_roundtrip(tmp_path: Path):
    controller, gate = make_controller(tmp_path)
    target_dir = tmp_path / "pasta nova"
    mkdir_id = await grant_for(controller, gate, "mkdir", str(target_dir))
    result = await controller.fs_mkdir(str(target_dir), mkdir_id)
    assert result["success"] and result["effect_verified"] and target_dir.is_dir()

    file_path = target_dir / "arquivo.txt"
    write_id = await grant_for(
        controller, gate, "fs_write", str(file_path),
        binding_digest=controller._binding_digest(False, "conteúdo KAZUMI çãí"),
    )
    written = await controller.fs_write(str(file_path), "conteúdo KAZUMI çãí",
                                        append=False, approval_id=write_id)
    assert written["success"] and written["effect_verified"]

    read = await controller.fs_read(str(file_path))
    assert read["success"] and "conteúdo KAZUMI" in read["content"]

    rename_id = await grant_for(
        controller, gate, "rename", str(file_path),
        binding_digest=controller._binding_digest("renomeado.txt"),
    )
    renamed = await controller.fs_rename(str(file_path), "renomeado.txt", rename_id)
    assert renamed["success"] and renamed["effect_verified"] and (target_dir / "renomeado.txt").is_file()


@pytest.mark.asyncio
async def test_fs_copy_move_delete_with_verification(tmp_path: Path):
    controller, gate = make_controller(tmp_path)
    source = tmp_path / "origem.txt"
    source.write_text("dados", encoding="utf-8")

    copy_id = await grant_for(controller, gate, "copy", f"{source} -> {tmp_path / 'destino.txt'}")
    copied = await controller.fs_copy(str(source), str(tmp_path / "destino.txt"), copy_id)
    assert copied["effect_verified"] and (tmp_path / "destino.txt").is_file()

    move_id = await grant_for(controller, gate, "move", f"{tmp_path / 'destino.txt'} -> {tmp_path / 'movido.txt'}")
    moved = await controller.fs_move(str(tmp_path / "destino.txt"), str(tmp_path / "movido.txt"), move_id)
    assert moved["effect_verified"] and (tmp_path / "movido.txt").exists() and not (tmp_path / "destino.txt").exists()

    delete_id = await grant_for(controller, gate, "fs_delete", str(tmp_path / "movido.txt"), timeout_seconds=180)
    deleted = await controller.fs_delete(str(tmp_path / "movido.txt"), delete_id)
    assert deleted["effect_verified"] and not (tmp_path / "movido.txt").exists()


@pytest.mark.asyncio
async def test_fs_delete_blocks_protected_paths_without_approval(tmp_path: Path):
    controller, _ = make_controller(tmp_path)
    result = await controller.fs_delete(str(tmp_path), approval_id=None)
    assert result["success"] is False
    # tmp_path tem poucos níveis, mas home/raiz/projeto são bloqueados por política;
    # aqui valida apenas que pedidos SEM approval retornam APPROVAL_REQUIRED antes.
    write_target = tmp_path / "x.txt"
    blocked = await controller.fs_write(str(write_target), "x", append=False, approval_id=None)
    assert blocked["error_code"] == "APPROVAL_REQUIRED"
    assert blocked.get("approval_id")


@pytest.mark.asyncio
async def test_fs_write_approval_binds_exact_content_and_append_mode(tmp_path: Path):
    controller, gate = make_controller(tmp_path)
    target = tmp_path / "bound.txt"
    pending = await controller.fs_write(str(target), "approved", append=False)
    gate.grant(pending["approval_id"], "test")

    tampered = await controller.fs_write(
        str(target), "changed", append=False, approval_id=pending["approval_id"],
    )
    assert tampered["error_code"] == "APPROVAL_REJECTED"
    assert not target.exists()

    exact = await controller.fs_write(
        str(target), "approved", append=False, approval_id=pending["approval_id"],
    )
    assert exact["success"] is True
    assert target.read_text(encoding="utf-8") == "approved"


@pytest.mark.asyncio
async def test_process_list_and_status(tmp_path: Path):
    controller, _ = make_controller(tmp_path)
    listing = await controller.process_list("memory", 10)
    assert listing["success"] and listing["processes"]
    names = [item["name"] for item in listing["processes"]]
    assert any(name for name in names)

    status = await controller.process_status(pid=None, name="python")
    if status["success"]:
        assert status["processes"][0]["pid"] > 0


@pytest.mark.asyncio
async def test_power_action_validation(tmp_path: Path):
    controller, _ = make_controller(tmp_path)
    invalid = await controller.system_power("explode")
    assert invalid["success"] is False and invalid["error_code"] == "INVALID_ACTION"

    shutdown_no_approval = await controller.system_power("shutdown", None)
    assert shutdown_no_approval["error_code"] == "APPROVAL_REQUIRED"


def test_browser_url_validation():
    from app.desktop.browser import BrowserController

    controller = BrowserController()

    async def run():
        return await controller.open("javascript:alert(1)")

    import asyncio

    result = asyncio.run(run())
    assert result["success"] is False and result["error_code"] == "INVALID_URL"


@pytest.mark.asyncio
async def test_browser_close_uses_verified_tab_disappearance_when_cdp_body_is_empty(monkeypatch):
    from app.desktop import browser as module

    controller = module.BrowserController()
    controller.manager.port = 9222
    controller.manager.browser = "chrome"
    async def ensure_running(_browser=""):
        return True, {"port": 9222, "browser": "chrome"}
    controller.manager.ensure_running = ensure_running
    controller.manager.status = lambda: {"reachable": True, "port": 9222}
    listings = iter([
        [{"id": "tab-1", "title": "Local", "url": "http://127.0.0.1", "type": "page"}],
        [],
    ])
    monkeypatch.setattr(module, "_tabs", lambda _port: next(listings))
    monkeypatch.setattr(module, "_http_json", lambda *_args, **_kwargs: (False, None))
    monkeypatch.setattr(module, "_sleep", lambda _seconds: _no_wait())

    result = await controller.close_tab("tab-1")

    assert result["success"] is True
    assert result["execution_success"] is True
    assert result["effect_verified"] is True


async def _no_wait():
    return None


def test_registry_hive_validation():
    from pathlib import Path as _Path

    import asyncio

    controller = OperatorController(EventBus(), ShellApprovalGate())
    result = asyncio.run(controller.registry_read("SOFTWARE\\Evil", "x"))
    assert result["success"] is False and result["error_code"] == "INVALID_KEY"
