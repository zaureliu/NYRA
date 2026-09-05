"""Event-driven Desktop Watcher (spec Parte I §173-§186).

Event sources, light by design (§174):
    - SetWinEventHook (real OS events) for window created/closed/focused/
      title_changed on a dedicated message-pump thread;
    - bounded psutil snapshots ONLY while a process watch is active;
    - bounded directory scans ONLY while a file watch is active;
    - per-service `sc query` polls ONLY while that service is watched;
    - device events answered honestly with CAPABILITY_UNAVAILABLE.

Rules: watches have TTL and expire (§180/§181); events are debounced (§186);
events NEVER trigger actions by themselves — only registered rules/tasks
consume them (§184/§185).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from typing import Any

from app.tools.redaction import redact_secrets

_WINDOW_EVENTS = {"window.created", "window.closed", "window.focused", "window.title_changed"}
_PROCESS_EVENTS = {"process.started", "process.exited"}
_FILE_EVENTS = {"file.created", "file.changed"}
_SERVICE_EVENTS = {"service.changed"}
_DEVICE_EVENTS = {"device.connected", "device.disconnected"}

_SUPPORTED = _WINDOW_EVENTS | _PROCESS_EVENTS | _FILE_EVENTS | _SERVICE_EVENTS | _DEVICE_EVENTS

_MAX_ACTIVE_WATCHES = 16
_DEFAULT_TTL_SECONDS = 300
_DEBOUNCE_SECONDS = 0.3


class WatchError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _new_watch_id() -> str:
    import secrets

    return f"watch_{secrets.token_hex(6)}"


class _Watch:
    __slots__ = ("watch_id", "event_types", "filters", "expires_at", "buffer",
                 "last_seen_key", "process_snapshot", "path_snapshot", "service_states")

    def __init__(self, watch_id: str, event_types: set[str], filters: dict[str, str],
                 ttl_seconds: float) -> None:
        self.watch_id = watch_id
        self.event_types = event_types
        self.filters = filters or {}
        self.expires_at = time.time() + ttl_seconds
        self.buffer: deque[dict] = deque(maxlen=200)
        self.last_seen_key: str = ""
        self.process_snapshot: dict[int, tuple[str, float]] = {}
        self.path_snapshot: dict[str, float] = {}
        self.service_states: dict[str, str] = {}

    def matches(self, event_type: str, subject: str) -> bool:
        if event_type not in self.event_types:
            return False
        app_filter = self.filters.get("app") or ""
        process_filter = self.filters.get("process") or ""
        path_filter = self.filters.get("path") or ""
        window_filter = self.filters.get("window") or ""
        if app_filter and app_filter.casefold() not in str(subject).casefold():
            return False
        if process_filter and process_filter.casefold() not in str(subject).casefold():
            return False
        if path_filter:
            base = re.escape(path_filter.rstrip("\\/"))
            if not re.match(base, str(subject), flags=re.IGNORECASE):
                return False
        if window_filter and window_filter.casefold() not in str(subject).casefold():
            return False
        return True

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def debounce_ok(self, key: str) -> bool:
        if key == self.last_seen_key:
            return False
        self.last_seen_key = key
        return True


class DesktopWatcher:
    def __init__(self, event_bus=None, *, default_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
                 max_active: int = _MAX_ACTIVE_WATCHES) -> None:
        self.event_bus = event_bus
        self.default_ttl_seconds = default_ttl_seconds
        self.max_active = max_active
        self._watches: dict[str, _Watch] = {}
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._hook_thread = None
        self._hook_queue: deque[tuple[str, int, int]] = deque(maxlen=512)
        self._pending_publish: list[tuple[str, str, dict]] = []
        self._last_debounce: float = 0.0
        self.metrics = {"events_emitted": 0, "watches_expired": 0}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks.append(asyncio.create_task(self._sweep_loop(), name="kazumi-watch-sweep"))
        self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="kazumi-watch-dispatch"))
        self._ensure_win_event_hook()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._watches.clear()

    # -------------------------------------------------------------------- register
    async def register(self, event_types: list[str], *, filters: dict[str, str] | None = None,
                       ttl_seconds: int | None = None) -> dict:
        wanted = {str(item).strip().casefold() for item in event_types if str(item).strip()}
        unsupported = wanted & _DEVICE_EVENTS
        if unsupported:
            return {"success": False, "error_code": "CAPABILITY_UNAVAILABLE",
                    "message": "Eventos de dispositivo não são suportados nesta versão (sem WM_DEVICECHANGE).",
                    "unsupported": sorted(unsupported)}
        invalid = wanted - _SUPPORTED
        if invalid:
            return {"success": False, "error_code": "INVALID_EVENT_TYPE",
                    "message": f"Tipos inválidos: {sorted(invalid)}", "supported": sorted(_SUPPORTED)}
        if not wanted:
            raise WatchError("EVENT_TYPES_REQUIRED", "Informe pelo menos um tipo de evento.")
        ttl = max(15.0, min(float(ttl_seconds or self.default_ttl_seconds), 3600.0))
        async with self._lock:
            self._sweep_locked()
            if len(self._watches) >= self.max_active:
                return {"success": False, "error_code": "WATCH_LIMIT",
                        "message": f"Limite de {self.max_active} watches ativos."}
            watch = _Watch(_new_watch_id(), wanted, dict(filters or {}), ttl)
            self._watches[watch.watch_id] = watch
        if (wanted & _PROCESS_EVENTS) or (wanted & _FILE_EVENTS) or (wanted & _SERVICE_EVENTS):
            await asyncio.to_thread(self._prime_snapshot, watch)
        return {"success": True, "watch_id": watch.watch_id,
                "event_types": sorted(wanted), "expires_at": watch.expires_at,
                "ttl_seconds": ttl}

    async def cancel(self, watch_id: str) -> dict:
        async with self._lock:
            removed = self._watches.pop(watch_id, None)
        if removed is None:
            return {"success": False, "error_code": "WATCH_NOT_FOUND"}
        return {"success": True, "cancelled": watch_id}

    def events(self, watch_id: str, after_index: int = 0) -> dict:
        watch = self._watches.get(watch_id)
        if watch is None:
            return {"success": False, "error_code": "WATCH_NOT_FOUND"}
        items = list(watch.buffer)[max(0, after_index):]
        return {"success": True, "watch_id": watch_id, "events": items[:100],
                "next_index": after_index + len(items)}

    def list_watches(self) -> dict:
        self._sweep_locked()
        items = [
            {"watch_id": watch.watch_id, "event_types": sorted(watch.event_types),
             "filters": watch.filters, "expires_at": watch.expires_at,
             "expired": watch.expired(), "buffered_events": len(watch.buffer)}
            for watch in self._watches.values()
        ]
        return {"success": True, "watches": items, "count": len(items)}

    # ------------------------------------------------------------------ dispatch
    def _emit(self, event_type: str, subject: str = "", **payload: Any) -> None:
        now = time.time()
        if now - self._last_debounce < _DEBOUNCE_SECONDS and event_type.startswith("window."):
            return  # §186: debounce global leve para rajadas de janela
        self._last_debounce = now
        for watch in list(self._watches.values()):
            if watch.expired(now):
                continue
            if not watch.matches(event_type, subject):
                continue
            key = f"{event_type}:{subject}"
            if not watch.debounce_ok(key):
                continue
            entry = {"type": event_type, "subject": redact_secrets(str(subject))[:160],
                     "timestamp": now, **{k: v for k, v in payload.items()}}
            watch.buffer.append(entry)
            self.metrics["events_emitted"] += 1
            self._publish(event_type, watch.watch_id, entry)

    def _publish(self, event_type: str, watch_id: str, entry: dict) -> None:
        """Queue for async publication (bus is async; emitters may be threads)."""
        self._pending_publish.append((event_type, watch_id, dict(entry)))

    async def _flush_publications(self) -> None:
        if not self._pending_publish or self.event_bus is None:
            self._pending_publish.clear()
            return
        from app.events import EventType

        try:
            event = EventType.DESKTOP_EVENT
        except ValueError:
            event = EventType.ERROR
        batch = list(self._pending_publish)
        self._pending_publish.clear()
        for event_type, watch_id, entry in batch[:64]:
            try:
                await self.event_bus.publish(event, watch_id=watch_id,
                                             event_type=event_type,
                                             subject=entry.get("subject", ""),
                                             payload=entry)
            except Exception:  # noqa: BLE001 - watcher nunca derruba o bus
                pass

    async def _dispatch_loop(self) -> None:
        """Drain WinEvent queue + poll scoped sources + flush publications."""
        while self._running:
            try:
                await asyncio.sleep(1.5)
                await asyncio.to_thread(self._drain_hook_queue)
                await asyncio.to_thread(self._poll_scoped_sources)
                await self._flush_publications()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                continue

    async def _sweep_loop(self) -> None:
        while self._running:
            await asyncio.sleep(5.0)
            async with self._lock:
                before = len(self._watches)
                self._sweep_locked()
                expired_count = before - len(self._watches)
            if expired_count:
                self.metrics["watches_expired"] += expired_count

    def _sweep_locked(self) -> None:
        expired = [wid for wid, watch in self._watches.items() if watch.expired()]
        for wid in expired:
            self._watches.pop(wid, None)

    # ------------------------------------------------------- win event hook (§175)
    def _ensure_win_event_hook(self) -> None:
        """One shared pump per process; started only when window watches exist."""
        from app.operator import win_events

        needed = any(watch.event_types & _WINDOW_EVENTS for watch in self._watches.values())
        if not needed:
            return
        if win_events._started:
            return

        def on_event(event: int, hwnd: int) -> None:
            self._hook_queue.append((int(event), int(hwnd or 0), time.time()))

        win_events.ensure_pump(on_event)

    def _drain_hook_queue(self) -> None:
        while self._hook_queue:
            event, hwnd, timestamp = self._hook_queue.popleft()
            title, process_name, class_name = _window_identity(hwnd)
            mapping = {
                0x8000: "window.created",
                0x8001: "window.closed",
                0x0003: "window.focused",
                0x800C: "window.title_changed",
            }
            event_type = mapping.get(event)
            if not event_type:
                continue
            subject = f"{process_name}:{title}"[:200]
            if event == 0x0003 and class_name and class_name.casefold() == "#32770":
                # §183: modal/error dialog surfaced as its own signal.
                self._emit("modal.detected", subject, hwnd=hwnd, window_class=class_name,
                           title=title[:120])
            self._emit(event_type, subject, hwnd=hwnd, title=title[:120],
                       process=process_name[:60], window_class=class_name)

    # ------------------------------------------------------------ scoped polling
    def _prime_snapshot(self, watch: _Watch) -> None:
        try:
            if watch.event_types & _PROCESS_EVENTS:
                watch.process_snapshot = {pid: (name, create_time)
                                          for pid, name, create_time in _list_processes()}
            if watch.event_types & _FILE_EVENTS and watch.filters.get("path"):
                watch.path_snapshot = _scan_directory(watch.filters["path"])
            if watch.event_types & _SERVICE_EVENTS:
                for service in (watch.filters.get("service") or "").split(","):
                    clean = service.strip()
                    if clean:
                        watch.service_states[clean] = _service_state(clean)
        except Exception:  # noqa: BLE001
            pass

    def _poll_scoped_sources(self) -> None:
        active = [watch for watch in self._watches.values() if not watch.expired()]
        if not active:
            return
        needs_process = any(w.event_types & _PROCESS_EVENTS for w in active)
        needs_files = any(w.event_types & _FILE_EVENTS for w in active)
        needs_services = any(w.event_types & _SERVICE_EVENTS for w in active)
        if needs_process:
            current = {pid: (name, create_time) for pid, name, create_time in _list_processes()}
            for watch in active:
                if not (watch.event_types & _PROCESS_EVENTS):
                    continue
                base = watch.process_snapshot or {}
                started = set(current) - set(base)
                exited = set(base) - set(current)
                for pid in started:
                    subject = current[pid][0]
                    if watch.filters.get("process") and watch.filters["process"].casefold() not in subject.casefold():
                        continue
                    self._emit("process.started", subject, pid=pid)
                for pid in exited:
                    subject = base[pid][0]
                    if watch.filters.get("process") and watch.filters["process"].casefold() not in subject.casefold():
                        continue
                    self._emit("process.exited", subject, pid=pid)
                watch.process_snapshot = current
        if needs_files:
            for watch in active:
                if not (watch.event_types & _FILE_EVENTS) or not watch.filters.get("path"):
                    continue
                path = watch.filters["path"]
                current = _scan_directory(path)
                base = watch.path_snapshot
                prefix = path.rstrip("\\/") + "\\"
                for name, mtime in current.items():
                    if name not in base:
                        self._emit("file.created", prefix + name,
                                   file=name, size_hint=None)
                    elif mtime != base[name]:
                        self._emit("file.changed", prefix + name, file=name)
                watch.path_snapshot = current
        if needs_services:
            for watch in active:
                if not (watch.event_types & _SERVICE_EVENTS):
                    continue
                for service in list(watch.service_states):
                    state = _service_state(service)
                    if state and state != watch.service_states[service]:
                        watch.service_states[service] = state
                        self._emit("service.changed", service, service_state=state)

    def status(self) -> dict:
        listing = self.list_watches()
        return {"success": True, "running": self._running,
                "supported_event_types": sorted(_SUPPORTED),
                "metrics": dict(self.metrics), **listing}


# ----------------------------------------------------------------- os helpers
def _list_processes(limit: int = 400) -> list[tuple[int, str, float]]:
    try:
        import psutil

        out = []
        for process in psutil.process_iter(["name", "create_time"]):
            info = process.info
            out.append((process.pid, info.get("name") or "", float(info.get("create_time") or 0)))
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _scan_directory(path: str, limit: int = 500) -> dict[str, float]:
    from pathlib import Path

    try:
        root = Path(path)
        if not root.is_dir():
            return {}
        entries: dict[str, float] = {}
        with os_scandir(root) as iterator:
            for entry in iterator:
                try:
                    if entry.is_file():
                        entries[entry.name] = entry.stat().st_mtime
                except OSError:
                    continue
                if len(entries) >= limit:
                    break
        return entries
    except OSError:
        return {}


def os_scandir(root):
    import os

    return os.scandir(root)


def _service_state(service: str) -> str:
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603
            ["sc.exe", "query", service], capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = completed.stdout.decode("utf-8", errors="replace")
        match = re.search(r"^\s+STATE\s+:\s+\d+\s+(\S+)", output, flags=re.MULTILINE)
        return match.group(1) if match else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _window_identity(hwnd: int) -> tuple[str, str, str]:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not hwnd or not user32.IsWindow(hwnd):
        return "", "", ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    class_buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buffer, 256)
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process_name = ""
    if pid.value:
        try:
            import psutil

            process_name = psutil.Process(pid.value).name() or ""
        except Exception:  # noqa: BLE001
            handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                name_buffer = ctypes.create_unicode_buffer(260)
                size = wt.DWORD(260)
                try:
                    kernel32.QueryFullProcessImageNameW(handle, 0, name_buffer, ctypes.byref(size))
                    process_name = name_buffer.value.rsplit("\\", 1)[-1]
                finally:
                    kernel32.CloseHandle(handle)
    return buffer.value, process_name, class_buffer.value
