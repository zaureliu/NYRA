"""Win32 top-level window enumeration (visible windows only) via ctypes."""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes

import psutil

from app.desktop.models import WindowInfo

_user32 = ctypes.windll.user32
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Hosts de console criam janelas visíveis com o cmdline/caminho do executável como
# título; elas NÃO são janelas de aplicação e nunca contam como evidência de GUI.
_CONSOLE_WINDOW_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"}
_PATHLIKE_TITLE = re.compile(r"^[A-Za-z]:\\[^\r\n]*\.(exe|com|bat|cmd|py|ps1)\s*$", re.IGNORECASE)


def _is_console_host_window(hwnd: wintypes.HWND, title: str) -> bool:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    if buffer.value.casefold() in _CONSOLE_WINDOW_CLASSES:
        return True
    return bool(_PATHLIKE_TITLE.match(title.strip()))


def _window_class(hwnd: wintypes.HWND) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def list_visible_windows() -> list[WindowInfo]:
    """Enumerate visible top-level APPLICATION windows of the interactive desktop."""
    collected: list[WindowInfo] = []

    def _on_window(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, class_buffer, 256)
        if _is_console_host_window(hwnd, buffer.value):
            return True
        pid_value = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
        collected.append(WindowInfo(
            hwnd=int(hwnd),
            pid=int(pid_value.value),
            title=buffer.value,
            visible=True,
            window_class=class_buffer.value,
        ))
        return True

    procedure = _EnumWindowsProc(_on_window)
    if not _user32.EnumWindows(procedure, 0):
        raise OSError("EnumWindows falhou")
    return collected


def annotate_process_names(windows: list[WindowInfo]) -> list[WindowInfo]:
    for window in windows:
        try:
            window.process_name = psutil.Process(window.pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            window.process_name = None
    return windows


def find_windows_for_app(
    *,
    process_names: list[str],
    title_contains: list[str],
    extra_pids: set[int] | None = None,
) -> list[WindowInfo]:
    """Match windows by owning process name or tracked PID; title fallback covers UWP hosts."""
    names = {name.casefold() for name in process_names if name}
    titles = [token.casefold() for token in title_contains if token]
    matches: list[WindowInfo] = []
    for window in annotate_process_names(list_visible_windows()):
        process_name = (window.process_name or "").casefold()
        pid_match = bool(extra_pids and window.pid in extra_pids) or process_name in names
        title_match = any(token in window.title.casefold() for token in titles) if titles else False
        if pid_match or title_match:
            matches.append(window)
    return matches
