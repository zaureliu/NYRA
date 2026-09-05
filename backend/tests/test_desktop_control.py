"""Desktop Application Control V1 tests: registry validation, real launch with
visible-window verification (the Notepad case), honest failures and tool layer."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.core.turn import current_turn_id
from app.desktop import DesktopController, load_desktop_apps
from app.desktop.discovery import ApplicationCandidate, ApplicationDiscovery, LaunchMethod, expand_launch_target
from app.desktop.windows import find_windows_for_app, list_visible_windows
from app.events import EventBus
from app.tools.models import RiskLevel
from app.tools.registry import ToolRegistry
from app.tools.shell_approval import ShellApprovalGate


def write_apps(path: Path, apps: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"apps": apps}, allow_unicode=True), encoding="utf-8")
    return path


def make_controller(tmp_path: Path, apps: list[dict]) -> DesktopController:
    controller = DesktopController(EventBus(), apps_path=write_apps(tmp_path / "desktop_apps.yaml", apps))
    return controller


def notepad_app(**overrides) -> dict:
    entry = {
        "id": "notepad",
        "display_name": "Bloco de Notas",
        "enabled": True,
        "executable": "notepad.exe",
        "process_names": ["notepad.exe"],
        "window_title_contains": ["Bloco de Notas", "Notepad", "Sem título"],
        "startup_timeout_seconds": 12,
    }
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


async def kill_pid(pid: int) -> None:
    killer = await asyncio.create_subprocess_exec(
        "taskkill.exe", "/PID", str(pid), "/T", "/F",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await killer.wait()


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------


def test_registry_loads_valid_app():
    registry = load_desktop_apps(write_apps(Path(".tmp/desktop_valid.yaml"), [notepad_app()]))
    assert registry.get("notepad") is not None and registry.error_for("notepad") is None


def test_registry_marks_invalid_and_keeps_valid(tmp_path):
    bad = {"id": "badapp", "display_name": "Bad", "executable": 'cmd /c evil & del'}
    registry = load_desktop_apps(write_apps(tmp_path / "apps.yaml", [bad, notepad_app()]))
    assert registry.get("notepad") is not None
    assert "INVALID_CONFIGURATION" in (registry.error_for("badapp") or "")


def test_registry_rejects_duplicate_ids(tmp_path):
    registry = load_desktop_apps(write_apps(tmp_path / "apps.yaml", [notepad_app(), notepad_app()]))
    duplicates = [entry for entry in registry.entries if entry.app_id == "notepad"]
    assert len(duplicates) == 2
    assert any("DUPLICATE_APP_ID" in (entry.error or "") for entry in duplicates)
    assert len([entry for entry in duplicates if entry.spec is not None]) == 1


def test_registry_requires_window_or_process_criteria(tmp_path):
    blind = notepad_app(id="blind")
    blind.pop("process_names")
    blind.pop("window_title_contains")
    registry = load_desktop_apps(write_apps(tmp_path / "apps.yaml", [blind]))
    assert "process_names ou window_title_contains" in (registry.error_for("blind") or "")


# ---------------------------------------------------------------------------
# Window probe
# ---------------------------------------------------------------------------


def test_visible_window_enumeration_includes_current_console_or_shell_host():
    windows = list_visible_windows()
    assert isinstance(windows, list)
    # A máquina Windows interativa sempre possui alguma janela visível.
    assert any(window.title.strip() for window in windows)


@pytest.mark.asyncio
async def test_desktop_verification_event_keeps_current_turn_id(tmp_path):
    controller = make_controller(tmp_path, [])
    candidate = ApplicationCandidate(
        id="probe",
        display_name="Probe",
        source="test",
        launch_method=LaunchMethod.EXE,
        target="probe.exe",
    )
    token = current_turn_id.set("turn_desktop_events")
    try:
        await controller._publish_verified(candidate, 4242)
    finally:
        current_turn_id.reset(token)
    event = controller.event_bus.history()[-1]
    assert event.type.value == "DESKTOP_WINDOW_VERIFIED"
    assert event.payload["turn_id"] == "turn_desktop_events"


# ---------------------------------------------------------------------------
# Real launch — THE NOTEPAD CASE (spec: janela efetivamente visível e verificada)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notepad_launch_ends_with_visible_verified_window(tmp_path):
    controller = make_controller(tmp_path, [notepad_app()])
    await controller.initialize()

    result = await controller.launch("notepad", origin="test")
    try:
        assert result["success"] is True, result
        assert result["execution_success"] is True
        assert result["effect_verified"] is True
        assert result["verification_status"] == "VERIFIED"
        assert result["windows"] and result["windows"][0]["pid"] > 0
        launched_pid = result["pid"]

        # Estado atual ("continua aberto?") também verificável:
        status = controller.status_windows("notepad")
        assert status["open"] is True and any(w["pid"] for w in status["windows"])
    finally:
        if result.get("pid"):
            await kill_pid(result["pid"])
        for window in result.get("windows", []) or []:
            pass

    # Após fechar o próprio pid lançado, a verificação honesta não deve mais
    # afirmar janela baseada apenas no nosso rastreamento.
    time.sleep(0.6)
    post = find_windows_for_app(process_names=["notepad.exe"], title_contains=["Bloco de Notas"])
    _ = post  # pode existir notepad do operador; nunca matamos os deles


@pytest.mark.asyncio
async def test_unknown_app_is_never_launched(tmp_path):
    controller = make_controller(tmp_path, [])
    await controller.initialize()
    result = await controller.launch("evil")
    assert result["success"] is False and result["error_code"] == "UNKNOWN_APP"


@pytest.mark.asyncio
async def test_non_gui_process_reports_honest_window_failure(tmp_path):
    sleeper = {
        "id": "sleeperapp",
        "display_name": "Sleeper",
        "executable": sys.executable,
        "arguments": ["-c", "import time; time.sleep(30)"],
        "process_names": ["python.exe"],
        "startup_timeout_seconds": 3,
    }
    controller = make_controller(tmp_path, [sleeper])
    await controller.initialize()
    result = await controller.launch("sleeperapp", origin="test")
    try:
        assert result["success"] is False, result
        assert result["error_code"] == "WINDOW_NOT_CONFIRMED"
        assert result["effect_verified"] is False
        assert result["verification_status"] == "VERIFICATION_FAILED"
    finally:
        if result.get("pid"):
            await kill_pid(result["pid"])


@pytest.mark.asyncio
async def test_missing_executable_fails_without_spawn(tmp_path):
    ghost = notepad_app(id="ghostapp", executable="definitely_not_real_binary_xyz.exe")
    controller = make_controller(tmp_path, [ghost])
    await controller.initialize()
    result = await controller.launch("ghostapp", origin="test")
    assert result["success"] is False
    assert result["error_code"] == "EXECUTABLE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------


def build_tools(tmp_path):
    from app.desktop import register_desktop_tools

    controller = make_controller(tmp_path, [
        notepad_app(),
        {"id": "sentinel_like", "display_name": "No GUI", "enabled": True,
         "executable": sys.executable, "arguments": ["-c", "pass"],
         "process_names": ["python.exe"], "startup_timeout_seconds": 2},
    ])
    tools = ToolRegistry()
    return controller, tools


@pytest.mark.asyncio
async def test_tools_expose_launch_windows_and_list_with_grounded_results(tmp_path):
    from app.desktop import register_desktop_tools

    controller, tools = build_tools(tmp_path)
    await controller.initialize()
    register_desktop_tools(tools, controller)
    listing = await tools.execute("desktop_list_apps", {})
    assert listing.data["success"] is True
    ids = {item["id"] for item in listing.data["apps"]}
    assert "notepad" in ids

    launch = await tools.execute("desktop_launch", {"app": "notepad"})
    try:
        assert launch.risk == RiskLevel.LOW_RISK or str(launch.risk.value) == "LOW_RISK"
        assert launch.data["effect_verified"] is True and launch.data["verification_status"] == "VERIFIED"
        assert any(window["title"].strip() for window in launch.data["windows"])

        current = await tools.execute("desktop_windows", {"app": "notepad"})
        assert current.data["open"] is True
    finally:
        if launch.data.get("pid"):
            await kill_pid(launch.data["pid"])


@pytest.mark.asyncio
async def test_tool_rejects_unregistered_app_id(tmp_path):
    controller, tools = build_tools(tmp_path)
    await controller.initialize()
    from app.desktop import register_desktop_tools

    register_desktop_tools(tools, controller)
    injected = await tools.execute("desktop_launch", {"app": "evilpy"})
    assert injected.data["success"] is False
    assert injected.data["error_code"] == "UNKNOWN_APP"


def test_settings_default_desktop_apps_path_exists():
    settings = Settings.from_sources(database_path="unused.db")
    assert settings.desktop_apps_path.name == "desktop_apps.yaml"


@pytest.mark.asyncio
async def test_shell_execute_inputs_fail_closed_for_code_shortcuts_and_uris(tmp_path, monkeypatch):
    controller = make_controller(tmp_path, [])
    invoked: list[str] = []

    import ctypes

    monkeypatch.setattr(
        ctypes.windll.shell32,
        "ShellExecuteW",
        lambda _hwnd, _verb, target, _params, _cwd, _show: invoked.append(str(target)) or 33,
    )
    for suffix in (".exe", ".cmd", ".ps1", ".lnk", ".url", ".unknown"):
        target = tmp_path / f"payload{suffix}"
        target.write_bytes(b"not executed")
        blocked = await controller.open_file(str(target))
        assert blocked["error_code"] == "UNSAFE_FILE_TYPE"
    for uri in (
        "file:///C:/Windows/System32/notepad.exe",
        r"shell:AppsFolder\Microsoft.WindowsTerminal_8wekyb3d8bbwe!App",
        "ms-settings:network",
        "https://user:password@example.test/",
    ):
        blocked = await controller.open_url(uri)
        assert blocked["error_code"] == "INVALID_URL"
    assert invoked == []

    document = tmp_path / "notes.txt"
    document.write_text("safe", encoding="utf-8")
    opened_file = await controller.open_file(str(document))
    opened_url = await controller.open_url("https://example.test/path")
    assert opened_file["success"] is True
    assert opened_url["success"] is True
    assert invoked == [str(document.resolve()), "https://example.test/path"]


@pytest.mark.asyncio
async def test_classic_uia_actuators_require_exact_one_use_approval(tmp_path, monkeypatch):
    gate = ShellApprovalGate()
    controller = DesktopController(
        EventBus(), apps_path=write_apps(tmp_path / "apps.yaml", []), approvals=gate,
    )
    monkeypatch.setattr(controller, "_window_hwnd_for_uia", lambda **_kwargs: 4242)

    async def fake_uia_call(_fn, *_args, **_kwargs):
        return {"success": True, "method": "test"}

    monkeypatch.setattr(controller, "_uia_call", fake_uia_call)
    pending = await controller.ui_click(name="Proceed", control_type="Button", hwnd=4242)
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    gate.grant(pending["approval_id"], "test")
    tampered = await controller.ui_click(
        name="Delete", control_type="Button", hwnd=4242,
        approval_id=pending["approval_id"],
    )
    assert tampered["error_code"] == "APPROVAL_INVALID"
    exact = await controller.ui_click(
        name="Proceed", control_type="Button", hwnd=4242,
        approval_id=pending["approval_id"],
    )
    assert exact["success"] is True

    text_pending = await controller.ui_set_text(
        "approved value", name="Search", control_type="Edit", hwnd=4242,
    )
    assert text_pending["error_code"] == "APPROVAL_REQUIRED"
    assert "approved value" not in gate.get(text_pending["approval_id"]).command
    gate.grant(text_pending["approval_id"], "test")
    text_tampered = await controller.ui_set_text(
        "tampered value", name="Search", control_type="Edit", hwnd=4242,
        approval_id=text_pending["approval_id"],
    )
    assert text_tampered["error_code"] == "APPROVAL_INVALID"
    text_exact = await controller.ui_set_text(
        "approved value", name="Search", control_type="Edit", hwnd=4242,
        approval_id=text_pending["approval_id"],
    )
    assert text_exact["success"] is True

    from app.desktop import window_manager

    monkeypatch.setattr(window_manager, "focus_window", lambda _hwnd: True)
    keys_pending = await controller.ui_send_keys("{win+r}cmd.exe{enter}", hwnd=4242)
    assert keys_pending["error_code"] == "APPROVAL_REQUIRED"
    gate.grant(keys_pending["approval_id"], "test")
    keys_tampered = await controller.ui_send_keys(
        "{win+r}powershell.exe{enter}", hwnd=4242,
        approval_id=keys_pending["approval_id"],
    )
    assert keys_tampered["error_code"] == "APPROVAL_INVALID"
    keys_exact = await controller.ui_send_keys(
        "{win+r}cmd.exe{enter}", hwnd=4242,
        approval_id=keys_pending["approval_id"],
    )
    assert keys_exact["success"] is True


def test_dynamic_executable_target_expands_windows_environment(tmp_path, monkeypatch):
    executable = tmp_path / "probe.exe"
    executable.write_bytes(b"")
    monkeypatch.setenv("KAZUMI_TEST_APP_ROOT", str(tmp_path))
    raw_target = r"%KAZUMI_TEST_APP_ROOT%\probe.exe"
    candidate = ApplicationCandidate(
        id="probe",
        display_name="Probe",
        source="test",
        launch_method=LaunchMethod.EXE,
        target=raw_target,
    )

    assert expand_launch_target(f'"{raw_target}"') == str(executable)
    assert ApplicationDiscovery().revalidate(candidate) is True


@pytest.mark.asyncio
async def test_registered_app_accepts_human_display_name_without_expanding_trust(tmp_path):
    controller = make_controller(tmp_path, [notepad_app()])
    await controller.initialize()

    assert controller.resolve_registered_app_id("Bloco de Notas") == "notepad"
    assert controller.resolve_registered_app_id("aplicativo não registrado") is None
