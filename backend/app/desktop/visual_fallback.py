"""Local visual fallback for accessibility-poor editable application surfaces.

The fallback is intentionally narrow: it only targets a broad editable band
near the bottom of the already-owned foreground window.  Coordinates are
derived from the current captured window, never stored as app-specific screen
positions.  Every mutation is verified by a pixel delta restricted to that
same band.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VisualEditSurface:
    x: int
    y: int
    width: int
    height: int
    click_x: int
    click_y: int


def _pixel(frame, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * frame.width + x) * 4
    blue, green, red = frame.pixels[offset:offset + 3]
    return int(red), int(green), int(blue)


def locate_bottom_edit_surface(frame) -> VisualEditSurface | None:
    """Find a low-texture lower content band in a current window capture."""
    if frame.width < 320 or frame.height < 240:
        return None
    left = max(12, int(frame.width * 0.22))
    right = min(frame.width - 12, int(frame.width * 0.97))
    top = max(12, int(frame.height * 0.72))
    # Leave taskbar/status/bottom-resize chrome out of the editable search.
    bottom = min(frame.height - 12, int(frame.height * 0.925))
    if right - left < 180 or bottom - top < 40:
        return None

    candidates: list[tuple[float, int]] = []
    for y in range(top, bottom, 3):
        colors = [_pixel(frame, x, y) for x in range(left, right, 8)]
        if len(colors) < 16:
            continue
        brightness = sum(sum(color) / 3 for color in colors) / len(colors)
        transitions = sum(
            abs(colors[index][0] - colors[index - 1][0])
            + abs(colors[index][1] - colors[index - 1][1])
            + abs(colors[index][2] - colors[index - 1][2])
            for index in range(1, len(colors))
        ) / max(1, len(colors) - 1)
        # Editable bands are usually broad and comparatively flat.  Favor the
        # lowest credible row while rejecting pure black/window borders.
        if 12 <= brightness <= 245 and transitions <= 72:
            lower_bias = (y - top) / max(1, bottom - top)
            candidates.append((lower_bias * 80 - transitions, y))
    if not candidates:
        return None
    _score, click_y = max(candidates)
    band_height = max(32, int(frame.height * 0.075))
    band_top = max(top, click_y - band_height // 2)
    band_bottom = min(frame.height - 8, band_top + band_height)
    click_y = min(band_bottom - 8, max(band_top + 8, click_y))
    click_x = left + int((right - left) * 0.62)
    return VisualEditSurface(
        x=left,
        y=band_top,
        width=right - left,
        height=band_bottom - band_top,
        click_x=click_x,
        click_y=click_y,
    )


def region_changed(before, after, surface: VisualEditSurface) -> dict[str, Any]:
    if before.width != after.width or before.height != after.height:
        return {"verified": False, "changed_samples": 0, "ratio": 0.0}
    changed = 0
    total = 0
    for y in range(surface.y, surface.y + surface.height, 2):
        for x in range(surface.x, surface.x + surface.width, 2):
            left = _pixel(before, x, y)
            right = _pixel(after, x, y)
            total += 1
            if sum(abs(left[index] - right[index]) for index in range(3)) >= 36:
                changed += 1
    ratio = changed / max(1, total)
    return {
        "verified": changed >= 8 and ratio <= 0.35,
        "changed_samples": changed,
        "ratio": round(ratio, 6),
    }


async def type_on_visual_surface(hwnd: int, text: str, *, send: bool = False) -> dict[str, Any]:
    """Focus, click a derived edit surface, type literally and verify pixels."""
    from app.desktop import uia, window_manager as wm
    from app.operator.vision_capture import capture_window

    if not text or len(text) > 2000:
        return {"success": False, "effect_verified": False,
                "error_code": "INVALID_TEXT"}
    if not await asyncio.to_thread(wm.focus_window, hwnd):
        return {"success": False, "effect_verified": False,
                "error_code": "FOCUS_NOT_CONFIRMED"}
    try:
        before = await asyncio.to_thread(capture_window, hwnd)
    except Exception as error:  # noqa: BLE001
        return {"success": False, "effect_verified": False,
                "error_code": getattr(error, "code", "CAPTURE_FAILED")}
    surface = locate_bottom_edit_surface(before)
    if surface is None:
        return {"success": False, "effect_verified": False,
                "error_code": "VISUAL_EDIT_SURFACE_NOT_FOUND"}
    state = await asyncio.to_thread(wm.window_state, hwnd)
    rect = state.get("rect") or {}
    screen_x = int(rect.get("x") or 0) + surface.click_x
    screen_y = int(rect.get("y") or 0) + surface.click_y
    await asyncio.to_thread(uia._mouse_click, screen_x, screen_y)
    await asyncio.sleep(0.08)
    try:
        typed = await asyncio.to_thread(
            uia.send_keys_to_foreground,
            text,
            hwnd,
            interpret_sequences=False,
        )
        await asyncio.sleep(0.18)
        after_type = await asyncio.to_thread(capture_window, hwnd)
    except Exception as error:  # noqa: BLE001
        return {"success": False, "effect_verified": False,
                "error_code": getattr(error, "code", "VISUAL_INPUT_FAILED")}
    typed_delta = region_changed(before, after_type, surface)
    if not typed.get("success") or not typed_delta["verified"]:
        return {
            "success": False,
            "effect_verified": False,
            "error_code": "VISUAL_TYPE_NOT_CONFIRMED",
            "typed_delta": typed_delta,
            "surface": surface.__dict__,
        }
    evidence: dict[str, Any] = {
        "success": True,
        "effect_verified": True,
        "verification_status": "VERIFIED",
        "typed_delta": typed_delta,
        "surface": surface.__dict__,
        "method": "window_relative_visual_surface",
    }
    if not send:
        return evidence
    try:
        submitted = await asyncio.to_thread(
            uia.send_keys_to_foreground, "{enter}", hwnd,
        )
        await asyncio.sleep(0.25)
        after_send = await asyncio.to_thread(capture_window, hwnd)
    except Exception as error:  # noqa: BLE001
        return {**evidence, "success": False, "effect_verified": False,
                "error_code": getattr(error, "code", "VISUAL_SEND_FAILED")}
    send_delta = region_changed(after_type, after_send, surface)
    evidence.update({
        "success": bool(submitted.get("success") and send_delta["verified"]),
        "effect_verified": bool(submitted.get("success") and send_delta["verified"]),
        "verification_status": (
            "VERIFIED" if submitted.get("success") and send_delta["verified"]
            else "VERIFICATION_FAILED"
        ),
        "send_delta": send_delta,
    })
    return evidence
