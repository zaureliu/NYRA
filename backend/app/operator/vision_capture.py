"""Screen capture primitives (spec Parte A §11-§34).

Pure ctypes/GDI — no new dependencies. Frames live in memory only with TTL;
PNG export exists for opt-in DEBUG persistence and for the OCR fallback
pipeline. Privacy: captures never leave the host unless debug mode is enabled.
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.tools.redaction import redact_secrets

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_PW_RENDERFULLCONTENT = 0x00000002
_DIB_RGB_COLORS = 0


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class CaptureError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class Frame:
    frame_id: str
    timestamp: float
    monitor_id: int
    window_handle: int
    width: int
    height: int
    pixels: bytes          # BGRA top-down rows
    scale: float = 1.0
    scope: str = "window"
    elements: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.timestamp)


class FrameStore:
    """TTL'd in-memory frame registry (§21/§22/§32): ids expire, LRU bounded."""

    def __init__(self, ttl_seconds: float = 45.0, max_frames: int = 8) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_frames = max_frames
        self._frames: OrderedDict[str, Frame] = OrderedDict()

    def put(self, frame: Frame) -> Frame:
        self.sweep()
        while len(self._frames) >= self.max_frames:
            self._frames.popitem(last=False)
        self._frames[frame.frame_id] = frame
        return frame

    def get(self, frame_id: str) -> Frame:
        self.sweep()
        if frame_id not in self._frames:
            raise CaptureError("FRAME_EXPIRED", "Frame inexistente ou expirado; capture novamente.")
        return self._frames[frame_id]

    def peek(self, frame_id: str) -> Frame | None:
        return self._frames.get(frame_id)

    def list_ids(self) -> list[dict]:
        self.sweep()
        return [
            {"frame_id": fid, "age_seconds": round(frame.age_seconds, 2), "scope": frame.scope,
             "window_handle": frame.window_handle,
             "dimensions": {"width": frame.width, "height": frame.height}}
            for fid, frame in self._frames.items()
        ]

    def drop(self, frame_id: str) -> bool:
        return self._frames.pop(frame_id, None) is not None

    def sweep(self) -> int:
        expired = [fid for fid, fr in self._frames.items() if fr.age_seconds > self.ttl_seconds]
        for fid in expired:
            self._frames.pop(fid, None)
        return len(expired)

    def clear(self) -> None:
        self._frames.clear()


# ------------------------------------------------------------------ GDI capture
def _screen_size() -> tuple[int, int]:
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def _monitor_rects() -> list[dict]:
    rects: list[dict] = []

    def _callback(hdc: int, hdc_frame: int, lprc: int, lparam: int) -> int:
        rect = ctypes.cast(lprc, ctypes.POINTER(_RECT)).contents
        rects.append({"monitor_id": len(rects) + 1,
                      "x": rect.left, "y": rect.top,
                      "width": rect.right - rect.left, "height": rect.bottom - rect.top})
        return 1

    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_long, ctypes.c_long, ctypes.POINTER(_RECT), ctypes.c_long)
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    if not rects:
        width, height = _screen_size()
        rects = [{"monitor_id": 1, "x": 0, "y": 0, "width": width, "height": height}]
    return rects


def _bitblt_region(x: int, y: int, width: int, height: int, source_dc: int) -> bytes:
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = _DIB_RGB_COLORS
    bits_ptr = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), _DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
    if not dib or not bits_ptr.value:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)
        raise CaptureError("CAPTURE_FAILED", "CreateDIBSection falhou.")
    try:
        old = gdi32.SelectObject(mem_dc, dib)
        gdi32.BitBlt(mem_dc, 0, 0, width, height, source_dc, x, y, _SRCCOPY | _CAPTUREBLT)
        size = width * height * 4
        buffer = ctypes.create_string_buffer(size)
        ctypes.memmove(buffer, bits_ptr.value, size)
        gdi32.SelectObject(mem_dc, old)
        return buffer.raw
    finally:
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def capture_screen(region: dict[str, int] | None = None, monitor_id: int = 0) -> Frame:
    """Scoped desktop/monitor capture (§12/§14): one monitor by default."""
    monitors = _monitor_rects()
    if region is not None:
        try:
            x, y = int(region["x"]), int(region["y"])
            width, height = int(region["width"]), int(region["height"])
        except (KeyError, TypeError, ValueError):
            raise CaptureError("INVALID_REGION", "region requer x,y,width,height inteiros.")
        if not (0 < width <= 1920 and 0 < height <= 1080):
            raise CaptureError("INVALID_REGION", "Região máxima permitida: 1920x1080 (privacy boundary).")
        bounds = next((m for m in monitors if m["monitor_id"] == (monitor_id or 1)), monitors[0])
        pixels = _bitblt_region(x, y, width, height, user32.GetDC(0))
        return _make_frame(pixels, width, height, hwnd=0, monitor_id=bounds["monitor_id"], scope="region")
    monitor = next((m for m in monitors if m["monitor_id"] == max(1, monitor_id)), monitors[0])
    pixels = _bitblt_region(monitor["x"], monitor["y"], monitor["width"], monitor["height"],
                            user32.GetDC(0))
    return _make_frame(pixels, monitor["width"], monitor["height"], hwnd=0,
                       monitor_id=monitor["monitor_id"], scope="monitor")


def capture_window(hwnd: int) -> Frame:
    """Window-scoped capture via PrintWindow (preferred when target known, §13)."""
    if not hwnd or not user32.IsWindow(hwnd):
        raise CaptureError("WINDOW_NOT_FOUND", f"hwnd={hwnd} não é uma janela válida.")
    rect = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureError("WINDOW_NOT_FOUND", "GetWindowRect falhou.")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise CaptureError("WINDOW_INVALID_GEOMETRY", "Janela minimizada ou invisível.")
    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = _DIB_RGB_COLORS
    bits_ptr = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(window_dc, ctypes.byref(bmi), _DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
    if not dib or not bits_ptr.value:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)
        raise CaptureError("CAPTURE_FAILED", "CreateDIBSection falhou.")
    try:
        old = gdi32.SelectObject(mem_dc, dib)
        ok = user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)
        if not ok:
            user32.BitBlt(mem_dc, 0, 0, width, height, window_dc, 0, 0, _SRCCOPY | _CAPTUREBLT)
        size = width * height * 4
        buffer = ctypes.create_string_buffer(size)
        ctypes.memmove(buffer, bits_ptr.value, size)
        gdi32.SelectObject(mem_dc, old)
        pixels = buffer.raw
    finally:
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)
    return _make_frame(pixels, width, height, hwnd=hwnd, monitor_id=1, scope="window")


def _make_frame(pixels: bytes, width: int, height: int, *, hwnd: int, monitor_id: int, scope: str) -> Frame:
    from uuid import uuid4

    return Frame(
        frame_id=f"frame_{uuid4().hex[:12]}",
        timestamp=time.time(),
        monitor_id=monitor_id,
        window_handle=hwnd,
        width=width,
        height=height,
        pixels=pixels,
        scale=1.0,
        scope=scope,
    )


# ------------------------------------------------------------------ PNG encoder
def frame_to_png_bytes(frame: Frame) -> bytes:
    """Minimal stdlib PNG writer (BGRA→RGB rows, filter 0)."""
    raw = bytearray()
    row_stride = frame.width * 4
    for y in range(frame.height):
        raw.append(0)  # filter type none
        row_start = y * row_stride
        for x in range(frame.width):
            base = row_start + x * 4
            raw += bytes((frame.pixels[base + 2], frame.pixels[base + 1], frame.pixels[base]))
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def save_debug_png(frame: Frame, directory: Any) -> str:
    path = directory / f"{frame.frame_id}.png"
    path.write_bytes(frame_to_png_bytes(frame))
    return str(path)


# ------------------------------------------------------------------- difference
def diff_frames(before: Frame, after: Frame, cell: int = 16) -> dict:
    """Grid-based pixel comparison for visual verification (§27/§28)."""
    if (before.width, before.height) != (after.width, after.height):
        return {"success": False, "error_code": "FRAME_SIZE_MISMATCH",
                "message": "Frames com dimensões diferentes; compare capturas do mesmo alvo."}
    changed_cells: list[dict] = []
    cols = (before.width + cell - 1) // cell
    rows = (before.height + cell - 1) // cell
    stride_b, stride_a = before.width * 4, after.width * 4
    threshold = 24
    min_x, min_y, max_x, max_y = before.width, before.height, -1, -1
    total_changed = 0
    for row in range(rows):
        for col in range(cols):
            x0, y0 = col * cell, row * cell
            acc = 0
            samples = 0
            for yy in range(y0, min(y0 + cell, before.height), 4):
                base_b = yy * stride_b + x0 * 4
                base_a = yy * stride_a + x0 * 4
                for xx in range(x0, min(x0 + cell, before.width), 8):
                    offset = base_b + xx * 4
                    mirror = base_a + xx * 4
                    for channel in range(3):
                        acc += abs(before.pixels[offset + channel] - after.pixels[mirror + channel])
                        samples += 1
            if samples and acc / samples > threshold:
                total_changed += 1
                min_x, min_y = min(min_x, x0), min(min_y, y0)
                max_x, max_y = max(max_x, min(x0 + cell, before.width)), max(max_y, min(y0 + cell, before.height))
                changed_cells.append({"x": x0, "y": y0})
    area_ratio = round(total_changed / max(1, cols * rows), 4)
    bbox = None
    if max_x >= 0:
        bbox = {"x": min_x, "y": min_y, "width": max_x - min_x, "height": max_y - min_y}
    return {
        "success": True,
        "changed": total_changed > 0,
        "changed_cell_count": total_changed,
        "grid": {"cols": cols, "rows": rows, "cell": cell},
        "area_ratio": area_ratio,
        "bounding_box": bbox,
        "cells_sample": changed_cells[:40],
    }


def fingerprint_pixels(frame: Frame, cell: int = 32) -> str:
    """Exact SHA-256 identity used by sensitive frame revalidation (§24).

    ``cell`` remains accepted for API compatibility but is deliberately
    ignored: sampling is insufficient for an authorization boundary.
    """
    import hashlib

    digest = hashlib.sha256()
    del cell
    stride = frame.width * 4
    for value in (frame.width, frame.height, stride, len(frame.pixels)):
        digest.update(int(value).to_bytes(8, "little", signed=False))
    digest.update(frame.pixels)
    return digest.hexdigest()


def sanitize_visual_text(value: str) -> str:
    return redact_secrets(value or "")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEBUG_DIR_DEFAULT = os.path.join("data", "vision-debug")
