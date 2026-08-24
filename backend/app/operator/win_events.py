"""SetWinEventHook message-pump (§175): real OS window events without polling.

ONE pump thread per process, shared by every watcher instance, cleanly
stoppable via WM_QUIT posted to the owning thread. A hook failure degrades the
watcher to scoped polling instead of crashing anything."""

from __future__ import annotations

import ctypes
import threading

_WINEVENT_OUTOFCONTEXT = 0x0000
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_CREATE = 0x8000
_EVENT_OBJECT_DESTROY = 0x8001
_EVENT_OBJECT_NAMECHANGE = 0x800C
_WM_QUIT = 0x0012

_lock = threading.Lock()
_thread_id: int | None = None
_callback_ref = None  # keep CFUNCTYPE alive
_started = False


def _event_range() -> tuple[int, int]:
    return min(_EVENT_SYSTEM_FOREGROUND, _EVENT_OBJECT_CREATE), max(_EVENT_OBJECT_NAMECHANGE, _EVENT_OBJECT_DESTROY)


def ensure_pump(callback) -> bool:
    """Start the shared pump once; subsequent calls are no-ops."""
    global _thread_id, _callback_ref, _started

    with _lock:
        if _started and _thread_id:
            return True
        user32 = ctypes.windll.user32

        def runner() -> None:
            global _thread_id
            kernel32 = ctypes.windll.kernel32
            _thread_id = kernel32.GetCurrentThreadId()
            event_min, event_max = _event_range()
            hook = user32.SetWinEventHook(
                event_min, event_max, None, callback, 0, 0, _WINEVENT_OUTOFCONTEXT,
            )
            if not hook:
                return
            class _MSG(ctypes.Structure):
                _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                            ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                            ("time", ctypes.c_ulong),
                            ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]

            buffer = _MSG()
            pointer = ctypes.byref(buffer)
            while user32.GetMessageW(pointer, None, 0, 0) > 0:
                user32.TranslateMessage(pointer)
                user32.DispatchMessageW(pointer)
            user32.UnhookWinEvent(hook)

        _callback_ref = ctypes.WINFUNCTYPE(
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_longlong,
            ctypes.c_long, ctypes.c_long, ctypes.c_ulong, ctypes.c_ulong,
        )(_dispatch_stub(callback))
        thread = threading.Thread(target=runner, daemon=True, name="nyra-win-event-pump")
        thread.start()
        _started = True
        # aguarda thread registrar seu id para permitir shutdown limpo
        for _ in range(50):
            if _thread_id:
                break
            time_sleep(0.02)
        return True


def _dispatch_stub(user_callback):
    def on_event(hook: int, event: int, hwnd: int, id_object: int, id_child: int,
                 thread: int, time_ms: int) -> None:
        try:
            user_callback(event, hwnd or 0)
        except Exception:  # noqa: BLE001 - callback nunca derruba o pump
            pass
    return on_event


def stop_pump() -> None:
    """Post WM_QUIT so GetMessageW returns and the hook is unhooked."""
    global _started
    with _lock:
        if not (_started and _thread_id):
            return
        try:
            user32 = ctypes.windll.user32
            user32.PostThreadMessageW(_thread_id, _WM_QUIT, None, None)
        except Exception:  # noqa: BLE001
            pass
        _started = False


def time_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
