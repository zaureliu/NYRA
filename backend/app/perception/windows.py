from __future__ import annotations

import ctypes
from ctypes import wintypes
import re

import psutil

from app.perception.models import ForegroundApp, MouseState


APP_CLASSES = {
    "code.exe": "VS Code",
    "chrome.exe": "Browser",
    "msedge.exe": "Browser",
    "firefox.exe": "Browser",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "windowsterminal.exe": "Terminal",
    "cmd.exe": "Terminal",
    "discord.exe": "Discord",
    "utamosentinel.exe": "Utamo Sentinel",
    "nyra-desktop.exe": "NYRA",
}


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def classify_process(process: str) -> str:
    key = process.casefold()
    if key in APP_CLASSES:
        return APP_CLASSES[key]
    stem = re.sub(r"\.exe$", "", process, flags=re.IGNORECASE).strip()
    return stem[:40] if stem else "Unknown"


def foreground_app(include_title: bool = False) -> ForegroundApp:
    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    if not handle:
        return ForegroundApp()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    try:
        process = psutil.Process(pid.value).name()
    except (psutil.Error, OSError):
        process = "unknown"
    title = None
    if include_title:
        length = min(512, user32.GetWindowTextLengthW(handle) + 1)
        buffer = ctypes.create_unicode_buffer(max(1, length))
        user32.GetWindowTextW(handle, buffer, length)
        title = re.sub(r"[\x00-\x1f]+", " ", buffer.value).strip()[:120] or None
    return ForegroundApp(process=process, classification=classify_process(process), title=title)


def idle_seconds() -> float:
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000


def mouse_state(include_position: bool = True) -> MouseState:
    point = POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return MouseState()
    width = max(1, ctypes.windll.user32.GetSystemMetrics(78))
    height = max(1, ctypes.windll.user32.GetSystemMetrics(79))
    left = ctypes.windll.user32.GetSystemMetrics(76)
    top = ctypes.windll.user32.GetSystemMetrics(77)
    relative_x = max(-1.0, min(1.0, ((point.x - left) / width) * 2 - 1)) if include_position else None
    relative_y = max(-1.0, min(1.0, ((point.y - top) / height) * 2 - 1)) if include_position else None
    monitor = int(ctypes.windll.user32.MonitorFromPoint(point, 2))
    return MouseState(relative_x=relative_x, relative_y=relative_y, monitor=monitor)


def virtual_resolution() -> str:
    user32 = ctypes.windll.user32
    return f"{user32.GetSystemMetrics(78)}x{user32.GetSystemMetrics(79)}"
