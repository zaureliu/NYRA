"""Win32 window state manipulation with mandatory verification.

Every mutation (focus/minimize/maximize/restore/move/resize/close) performs the
operation through real Win32 APIs and then RE-READS the window state to confirm
the effect before reporting success. Graceful close uses WM_CLOSE; force-kill
is never attempted here (spec §39-§41). NYRA's own components are protected
against accidental termination (spec §281-§285).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from app.desktop.models import WindowInfo

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_SW_RESTORE = 9

_GA_ROOT = 2

_RECT = wintypes.RECT


class WindowOperationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _root_owner(hwnd: int) -> int:
    return int(_user32.GetAncestor(hwnd, _GA_ROOT) or hwnd)


def window_state(hwnd: int) -> dict:
    """Read the full observable state of a top-level window."""
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    class_buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, class_buffer, 256)
    rect = _RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    pid_value = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
    foreground = _user32.GetForegroundWindow()
    return {
        "hwnd": int(hwnd),
        "title": buffer.value,
        "class_name": class_buffer.value,
        "pid": int(pid_value.value),
        "visible": bool(_user32.IsWindowVisible(hwnd)),
        "iconic": bool(_user32.IsIconic(hwnd)),
        "zoomed": bool(_user32.IsZoomed(hwnd)),
        "foreground": int(foreground or 0) == _root_owner(hwnd),
        "rect": {"x": rect.left, "y": rect.top, "width": rect.right - rect.left, "height": rect.bottom - rect.top},
        "alive": bool(_user32.IsWindow(hwnd)),
    }


def wait_for(predicate, timeout_seconds: float = 3.0, interval: float = 0.12) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def focus_window(hwnd: int, timeout_seconds: float = 3.0) -> bool:
    """Bring a window to the foreground and VERIFY via GetForegroundWindow."""
    root = _root_owner(hwnd)
    _user32.ShowWindow(root, _SW_RESTORE if _user32.IsIconic(root) else 5)
    current_thread = _kernel32.GetCurrentThreadId()
    target_thread = _user32.GetWindowThreadProcessId(root, None)
    attached = False
    if current_thread != target_thread:
        attached = bool(_user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        _user32.BringWindowToTop(root)
        _user32.SetForegroundWindow(root)
    finally:
        if attached:
            _user32.AttachThreadInput(current_thread, target_thread, False)
    return wait_for(lambda: int(_user32.GetForegroundWindow() or 0) == root, timeout_seconds)


def minimize_window(hwnd: int, timeout_seconds: float = 3.0) -> bool:
    root = _root_owner(hwnd)
    _user32.ShowWindow(root, _SW_MINIMIZE)
    return wait_for(lambda: bool(_user32.IsIconic(root)), timeout_seconds)


def maximize_window(hwnd: int, timeout_seconds: float = 3.0) -> bool:
    root = _root_owner(hwnd)
    _user32.ShowWindow(root, _SW_MAXIMIZE)
    return wait_for(lambda: bool(_user32.IsZoomed(root)), timeout_seconds)


def restore_window(hwnd: int, timeout_seconds: float = 3.0) -> bool:
    root = _root_owner(hwnd)
    _user32.ShowWindow(root, _SW_RESTORE)
    return wait_for(lambda: not bool(_user32.IsIconic(root)) and not bool(_user32.IsZoomed(root)), timeout_seconds)


def move_window(hwnd: int, x: int, y: int, timeout_seconds: float = 3.0) -> bool:
    root = _root_owner(hwnd)
    if _user32.IsIconic(root):
        _user32.ShowWindow(root, _SW_RESTORE)
    rect = _RECT()
    _user32.GetWindowRect(root, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    _user32.SetWindowPos(root, 0, int(x), int(y), width, height, 0x0004)
    moved: list[bool] = []

    def check() -> bool:
        _user32.GetWindowRect(root, ctypes.byref(rect))
        moved.clear()
        moved.append(abs(rect.left - x) <= 8 and abs(rect.top - y) <= 8)
        return moved[0]

    return wait_for(check, timeout_seconds)


def resize_window(hwnd: int, width: int, height: int, timeout_seconds: float = 3.0) -> bool:
    root = _root_owner(hwnd)
    if _user32.IsIconic(root) or _user32.IsZoomed(root):
        _user32.ShowWindow(root, _SW_RESTORE)
    rect = _RECT()
    _user32.GetWindowRect(root, ctypes.byref(rect))
    _user32.SetWindowPos(root, 0, rect.left, rect.top, int(width), int(height), 0x0004)

    def check() -> bool:
        _user32.GetWindowRect(root, ctypes.byref(rect))
        current_w, current_h = rect.right - rect.left, rect.bottom - rect.top
        return abs(current_w - width) <= 8 and abs(current_h - height) <= 8

    return wait_for(check, timeout_seconds)


def graceful_close(hwnd: int, timeout_seconds: float = 5.0) -> bool:
    """Send WM_CLOSE to the root window and verify it disappeared (§39-40)."""
    root = _root_owner(hwnd)
    _user32.PostMessageW(root, 0x0010, 0, 0)  # WM_CLOSE
    return wait_for(lambda: not _user32.IsWindow(root), timeout_seconds)


def window_still_alive(hwnd: int) -> bool:
    return bool(_user32.IsWindow(hwnd))


def is_own_process(pid: int) -> bool:
    """True when pid belongs to a NYRA component that must not be killed."""
    import os

    if pid == os.getpid():
        return True
    try:
        import psutil

        process = psutil.Process(pid)
        name = (process.name() or "").casefold()
        exe = (process.exe() or "").casefold()
    except Exception:  # noqa: BLE001
        return False
    if "nyra" in name or "nyra" in exe:
        return True
    try:
        current_parent = psutil.Process(os.getpid()).parent()
        while current_parent is not None:
            if current_parent.pid == pid:
                return True
            current_parent = current_parent.parent()
    except Exception:  # noqa: BLE001
        pass
    return False


def enumerate_window_summaries(limit: int = 60) -> list[dict]:
    from app.desktop.windows import annotate_process_names, list_visible_windows

    summaries: list[dict] = []
    for window in annotate_process_names(list_visible_windows())[:limit]:
        state = window_state(window.hwnd)
        summaries.append({
            "hwnd": window.hwnd,
            "pid": window.pid,
            "title": state["title"],
            "process_name": window.process_name,
            "foreground": state["foreground"],
            "iconic": state["iconic"],
            "zoomed": state["zoomed"],
            "rect": state["rect"],
        })
    return summaries
