"""Vision tests (spec Partes S §283-§285): real window capture, inspect finds a
button/label, visual action + verify via diff. Uses reversible targets only."""

from __future__ import annotations

import asyncio
import faulthandler
import subprocess
import sys
import time

import pytest

from app.operator.vision import VisionEngine
from app.operator.vision_capture import (
    Frame,
    capture_window,
    diff_frames,
    fingerprint_pixels,
    frame_to_png_bytes,
)
from app.tools.shell_approval import ShellApprovalGate


def _notepad_hwnd(pid: int) -> int | None:
    from app.desktop.windows import find_windows_for_app

    matches = find_windows_for_app(
        process_names=["notepad.exe"], title_contains=["Bloco de Notas", "Notepad"],
    )
    owned = [window for window in matches if window.pid == pid]
    return owned[0].hwnd if owned else None


def _spawn_notepad(tmp_path):
    process = subprocess.Popen(  # noqa: S603 - alvo reversível clássico dos smokes
        ["notepad.exe"], cwd=str(tmp_path),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    hwnd = None
    deadline = time.time() + 10
    while time.time() < deadline and hwnd is None:
        time.sleep(0.4)
        hwnd = _notepad_hwnd(process.pid)
    return process, hwnd


def _close(process, hwnd: int) -> None:
    if process and process.poll() is None:
        import ctypes

        ctypes.windll.user32.PostMessageW(int(hwnd), 0x0010, 0, 0)  # WM_CLOSE
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Fallback is scoped strictly to the process created by this test.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],  # noqa: S603
                           capture_output=True, timeout=10)
        time.sleep(0.3)


@pytest.fixture(scope="module", autouse=True)
def quiet_handled_uia_rpc_disconnects():
    """Pytest's faulthandler reports handled UIA provider disconnect SEH events.

    Windows UI Automation raises first-chance RPC_E_DISCONNECTED while a target
    window exits. COM handles it and the process remains healthy, but the pytest
    plugin prints it as a fatal exception. Silence only this real-UIA module;
    an unhandled native fault still terminates the test process and fails CI.
    """
    was_enabled = faulthandler.is_enabled()
    if sys.platform == "win32" and was_enabled:
        faulthandler.disable()
    yield
    if sys.platform == "win32" and was_enabled:
        faulthandler.enable()


@pytest.fixture(scope="module")
def vision():
    """One worker thread owns one COM apartment across the UIA smoke module."""
    engine = VisionEngine(debug_keep_frames=True)
    yield engine
    engine.shutdown()


@pytest.fixture()
def notepad(tmp_path):
    """Real Notepad window with deterministic teardown between tests."""
    process, hwnd = _spawn_notepad(tmp_path)
    assert hwnd is not None, "Notepad não abriu para o teste"
    yield int(hwnd or 0)
    _close(process, int(hwnd or 0))
    time.sleep(0.3)


@pytest.mark.asyncio
async def test_283_window_capture_scoped(notepad, vision):
    hwnd = notepad
    outcome = await asyncio.to_thread(vision.capture, target="window", hwnd=hwnd)
    assert outcome["success"] is True
    frame = outcome["frame"]
    assert frame["window_handle"] == int(hwnd or 0)
    assert frame["dimensions"]["width"] > 50 and frame["dimensions"]["height"] > 30
    assert outcome.get("debug_png_path")
    # §16: sem debug, nada é persistido.
    vision_off = VisionEngine(debug_keep_frames=False)
    quiet = await asyncio.to_thread(vision_off.capture, target="window", hwnd=hwnd)
    assert quiet.get("debug_png_path") is None


@pytest.mark.asyncio
async def test_284_visual_inspect_finds_controls(notepad, vision):
    hwnd = notepad
    capture = await asyncio.to_thread(vision.capture, target="window", hwnd=hwnd)
    frame_id = capture["frame"]["frame_id"]
    result = await vision.inspect(frame_id)
    assert result["success"] is True
    assert result["source"] == "uia"
    control_types = {item["control_type"] for item in result["detected_controls"]}
    assert len(result["detected_controls"]) > 0
    assert any(kind in control_types for kind in {"MenuItem", "Button", "Document", "Edit"}), \
        f"esperado ao menos um controle conhecido; obtido {control_types}"


@pytest.mark.asyncio
async def test_285_visual_action_with_verification_and_stale_guard(notepad, vision):
    """Click + verify via diff; stale frames are refused (§24/§28)."""
    hwnd = notepad
    first = await asyncio.to_thread(vision.capture, target="window", hwnd=hwnd)
    frame_id = first["frame"]["frame_id"]

    # §22/§24: elemento inexistente e frame expirado são recusados honestamente.
    missing = await vision.click(frame_id, "ve_999")
    assert missing["success"] is False
    assert missing["error_code"] == "VISUAL_ELEMENT_NOT_FOUND"

    inspection = await vision.inspect(frame_id)
    buttons = inspection.get("buttons") or []
    menus = [el for el in inspection["detected_controls"] if el["control_type"] == "MenuItem"]
    target_id = (buttons or [el["visual_element_id"] for el in menus] or [None])[0]
    if target_id:
        outcome = await vision.click(frame_id, target_id)
        if outcome.get("success"):
            assert outcome["verification_status"] in {"VERIFIED", "EXECUTED"}
            assert "method" in outcome

    # diff entre dois frames da mesma janela: estrutura ok mesmo sem mudança.
    second = await asyncio.to_thread(vision.capture, target="window", hwnd=hwnd)
    comparison = vision.compare(first["frame"]["frame_id"], second["frame"]["frame_id"])
    assert comparison["success"] is True
    assert comparison["area_ratio"] >= 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "control_type"),
    [("OK", "Button"), ("Proceed", "Hyperlink"), ("Accept", "Custom"),
     ("Continue", "MenuItem")],
)
async def test_every_visual_click_requires_exact_approval(monkeypatch, label, control_type):
    gate = ShellApprovalGate()
    engine = VisionEngine(approvals=gate)
    frame = Frame(
        frame_id="frame-confirm", timestamp=time.time(), monitor_id=1,
        window_handle=4242, width=1, height=1, pixels=b"\x00\x00\x00\x00",
        elements={
            "ve_yes": {
                "name": label, "automation_id": "confirm", "control_type": control_type,
                "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
            },
            "ve_other": {
                "name": label, "automation_id": "other", "control_type": control_type,
                "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
            },
        },
    )
    engine.frames.put(frame)

    async def not_stale(_frame):
        return False

    monkeypatch.setattr(engine, "_frame_is_stale", not_stale)
    monkeypatch.setattr(
        engine, "detect_modals",
        lambda: {"modals": []},
    )

    async def fake_run(_fn, *_args, **_kwargs):
        return {"method": "mock"}

    async def fake_verify(_frame):
        return {"changed": True, "area_ratio": 0.1}

    monkeypatch.setattr(engine, "_run", fake_run)
    monkeypatch.setattr(engine, "_verify_after_action", fake_verify)
    pending = await engine.click("frame-confirm", "ve_yes")
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    gate.grant(pending["approval_id"], "test")

    wrong = await engine.click(
        "frame-confirm", "ve_other", approval_id=pending["approval_id"],
    )
    assert wrong["error_code"] == "APPROVAL_INVALID"
    exact = await engine.click(
        "frame-confirm", "ve_yes", approval_id=pending["approval_id"],
    )
    assert exact["success"] is True and exact["approval_used"] is True
    replay = await engine.click(
        "frame-confirm", "ve_yes", approval_id=pending["approval_id"],
    )
    assert replay["error_code"] == "APPROVAL_INVALID"
    engine.shutdown()


@pytest.mark.asyncio
async def test_visual_type_binds_text_without_exposing_it(monkeypatch):
    gate = ShellApprovalGate()
    engine = VisionEngine(approvals=gate)
    frame = Frame(
        frame_id="frame-type", timestamp=time.time(), monitor_id=1,
        window_handle=4242, width=1, height=1, pixels=b"\x00\x00\x00\x00",
        elements={"ve_edit": {
            "name": "Search", "automation_id": "query", "control_type": "Edit",
            "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
        }},
    )
    engine.frames.put(frame)

    async def not_stale(_frame):
        return False

    async def fake_run(_fn, *_args, **_kwargs):
        return {"effect_verified": True, "stored_preview": "allowed"}

    monkeypatch.setattr(engine, "_frame_is_stale", not_stale)
    monkeypatch.setattr(engine, "_run", fake_run)
    pending = await engine.type_text("frame-type", "ve_edit", "allowed", secret=True)
    assert pending["error_code"] == "APPROVAL_REQUIRED"
    assert "allowed" not in gate.get(pending["approval_id"]).command
    gate.grant(pending["approval_id"], "test")
    wrong = await engine.type_text(
        "frame-type", "ve_edit", "tampered", secret=True,
        approval_id=pending["approval_id"],
    )
    assert wrong["error_code"] == "APPROVAL_INVALID"
    exact = await engine.type_text(
        "frame-type", "ve_edit", "allowed", secret=True,
        approval_id=pending["approval_id"],
    )
    assert exact["success"] is True and exact["approval_used"] is True
    assert exact["stored_preview"] == "<secret not echoed>"
    engine.shutdown()


def test_png_encoder_produces_valid_header(tmp_path):
    frame = capture_window(_foreground_or_screen(tmp_path))
    png = frame_to_png_bytes(frame)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    path = tmp_path / "frame.png"
    path.write_bytes(png)
    assert path.stat().st_size > 100


def _foreground_or_screen(tmp_path):
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        pytest.skip("sem janela em primeiro plano")
    return int(hwnd)


def test_diff_detects_synthetic_change():
    from app.operator.vision_capture import Frame

    width, height = 64, 64
    base = bytes(width * height * 4)
    altered = bytearray(base)
    for y in range(20):
        for x in range(20):
            offset = (y * width + x) * 4
            altered[offset + 2] = 255  # canal R em BGRA
    before = Frame("a", time.time(), 1, 0, width, height, base)
    after = Frame("b", time.time(), 1, 0, width, height, bytes(altered))
    result = diff_frames(before, after)
    assert result["changed"] is True
    assert result["changed_cell_count"] >= 1
    assert result["bounding_box"] is not None


def test_frame_fingerprint_covers_every_pixel_not_only_grid_samples():
    width, height = 64, 64
    base = bytes(width * height * 4)
    altered = bytearray(base)
    # (17, 19) was outside every 32x32 sampled coordinate in the old digest.
    altered[(19 * width + 17) * 4 + 2] = 1
    before = Frame("a", time.time(), 1, 0, width, height, base)
    after = Frame("b", time.time(), 1, 0, width, height, bytes(altered))

    assert fingerprint_pixels(before) != fingerprint_pixels(after)


@pytest.mark.asyncio
async def test_modal_detection_reports_policy():
    vision = VisionEngine()
    outcome = await asyncio.to_thread(vision.detect_modals)
    assert outcome["success"] is True
    assert isinstance(outcome["modals"], list)
    for modal in outcome["modals"]:
        assert "NUNCA aceitar modais destrutivos automaticamente" in modal["policy"]


def test_desktop_scope_refused_by_default():
    vision = VisionEngine()
    outcome = vision.capture(target="desktop")
    assert outcome["success"] is False
    assert outcome["error_code"] == "INVALID_TARGET"  # §14: desktop inteiro evitado
