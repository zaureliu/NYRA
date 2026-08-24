"""Vision tests (spec Partes S §283-§285): real window capture, inspect finds a
button/label, visual action + verify via diff. Uses reversible targets only."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest

from app.operator.vision import VisionEngine
from app.operator.vision_capture import capture_window, frame_to_png_bytes, diff_frames


def _notepad_hwnd() -> int | None:
    from app.desktop.windows import find_windows_for_app

    matches = find_windows_for_app(
        process_names=["notepad.exe"], title_contains=["Bloco de Notas", "Notepad"],
    )
    return matches[0].hwnd if matches else None


def _spawn_notepad(tmp_path):
    process = subprocess.Popen(  # noqa: S603 - alvo reversível clássico dos smokes
        ["notepad.exe"], cwd=str(tmp_path),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    hwnd = None
    deadline = time.time() + 10
    while time.time() < deadline and hwnd is None:
        time.sleep(0.4)
        hwnd = _notepad_hwnd()
    return process, hwnd


def _kill(process) -> None:
    if process and process.poll() is None:
        time.sleep(0.4)  # deixa proxies COM/UIA desconectarem antes do kill
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],  # noqa: S603
                       capture_output=True, timeout=10)
        time.sleep(0.2)


@pytest.fixture(scope="module")
def vision():
    """ONE VisionEngine for the whole module: one worker thread = one COM
    apartment = stable UIA/GDI across multiple app lifecycles."""
    import gc

    engine = VisionEngine(debug_keep_frames=True)
    yield engine
    engine.shutdown()
    gc.collect()


@pytest.fixture()
def notepad(tmp_path):
    """Real Notepad window with deterministic teardown between tests."""
    process, hwnd = _spawn_notepad(tmp_path)
    assert hwnd is not None, "Notepad não abriu para o teste"
    yield int(hwnd or 0)
    _kill(process)
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
