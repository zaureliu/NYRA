"""Camada 1 — ComputerPerceptionService (kazumi-7c §6-§16).

Consolida fontes DETERMINÍSTICAS existentes em snapshots compactos:
  processos/janelas (Win32+psutil), clipboard (só metadata), filesystem
  recente (limitado), UIA on-demand, browser honesto, rede/homelab via
  getters injetados, vision/OCR como fallback sob demanda.

Ordem de verdade (§5): Win32 → UIA → filesystem/service → browser →
registry → screen capture → OCR. Snapshot NUNCA captura pixels nem teclas.

Eventos normalizados (§16) com debounce/dedup via EventBus existente.
Polling leve e limitado (§81); nada aqui bloqueia o chat (§80).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.events import EventBus, EventType

logger = logging.getLogger("kazumi.computer.perception")

MAX_WINDOWS_IN_SNAPSHOT = 60
MAX_PROCESSES_IN_SNAPSHOT = 40
MAX_RECENT_FILES = 25
RECENT_FILE_ROOTS = ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos")

# Novos eventos normalizados (kazumi-7c §16) — aditivos ao EventType existente.
COMPUTER_EVENTS = {
    "WINDOW_FOREGROUND_CHANGED": "computer.window.foreground_changed",
    "WINDOW_OPENED": "computer.window.opened",
    "WINDOW_CLOSED": "computer.window.closed",
    "PROCESS_STARTED": "computer.process.started",
    "PROCESS_STOPPED": "computer.process.stopped",
    "FILE_CREATED": "computer.file.created",
    "FILE_MODIFIED": "computer.file.modified",
    "CLIPBOARD_CHANGED": "computer.clipboard.changed",
    "APPLICATION_LAUNCHED": "computer.application.launched",
    "APPLICATION_CLOSED": "computer.application.closed",
    "BROWSER_NAVIGATION": "computer.browser.navigation",
    "DIALOG_DETECTED": "computer.dialog.detected",
}


def register_computer_events() -> None:
    """Idempotente: registra os eventos computer.* no enum do EventBus."""
    for name, value in COMPUTER_EVENTS.items():
        if not hasattr(EventType, name):
            setattr(EventType, name, value)


register_computer_events()


@dataclass
class PerceptionConfig:
    refresh_interval_seconds: float = 2.0      # §81 max_perception_refresh_rate
    recent_files_enabled: bool = True
    clipboard_metadata_enabled: bool = True


class ComputerPerceptionService:
    """Snapshot determinístico do estado do computador + eventos normalizados."""

    def __init__(
        self,
        event_bus: EventBus,
        config: PerceptionConfig | None = None,
        *,
        homelab_summary_fn: Callable[[], dict] | None = None,
        network_status_fn: Callable[[], dict] | None = None,
        snapshot_consumer: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.event_bus = event_bus
        self.config = config or PerceptionConfig()
        self._homelab_summary_fn = homelab_summary_fn
        self._network_status_fn = network_status_fn
        self._snapshot_consumer = snapshot_consumer
        self.clock = clock
        self._task: asyncio.Task | None = None
        self._previous_windows: dict[int, dict] = {}
        self._previous_processes: dict[int, dict] = {}
        self._previous_files: dict[str, dict] = {}
        self._previous_clipboard_sig: tuple | None = None
        self._recent_files_cache: tuple[float, list[dict]] = (0.0, [])
        self._debounce: dict[str, float] = {}
        self._pending_events: list[tuple[str, dict]] = []
        self.last_snapshot: dict[str, Any] = {}
        self.metrics: dict[str, float] = {
            "perception_ms": 0.0,
            "perception_failure": 0.0,
        }

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        windows = self.windows_map()
        processes = self.processes_summary(windows)
        foreground_hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        foreground = next((w for w in windows if w["hwnd"] == foreground_hwnd), None)
        payload: dict[str, Any] = {
            "observed_at": self.clock(),
            "foreground_window": foreground,
            "windows": windows,
            "processes": processes,
            "clipboard": self.clipboard_metadata(),
            "recent_files": self.recent_files(),
            "browser": {"available": False, "reason": "sem sessão de automação ativa"},
            "homelab": self._safe(self._homelab_summary_fn),
            "network": self._safe(self._network_status_fn),
            "uia": {"mode": "on_demand"},
            "vision": {"mode": "fallback"},
            "ocr": {"mode": "fallback", "trust": "low_confidence_nunca_fato"},
        }
        self.last_snapshot = payload
        self.metrics["perception_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return payload

    @staticmethod
    def _safe(fn: Callable[[], dict] | None) -> dict:
        if fn is None:
            return {"available": False}
        try:
            data = fn()
            return data if isinstance(data, dict) else {"available": False}
        except Exception:  # noqa: BLE001 — percepção nunca derruba o chat
            return {"available": False}

    def windows_map(self) -> list[dict[str, Any]]:
        from app.desktop.window_manager import window_state
        from app.desktop.windows import annotate_process_names, list_visible_windows

        result: list[dict[str, Any]] = []
        for window in annotate_process_names(list_visible_windows())[:MAX_WINDOWS_IN_SNAPSHOT]:
            state = window_state(window.hwnd)
            result.append({
                "hwnd": window.hwnd,
                "pid": window.pid,
                "process": (window.process_name or "").casefold(),
                "title": state.get("title", ""),
                "window_class": window.window_class or state.get("class_name", ""),
                "visible": bool(state.get("visible")),
                "enabled": True,
                "minimized": bool(state.get("iconic")),
                "maximized": bool(state.get("zoomed")),
                "foreground": bool(state.get("foreground")),
                "bounds": state.get("rect", {}),
            })
        return result

    def processes_summary(self, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import psutil

        window_pids = {w["pid"] for w in windows}
        summaries: list[dict[str, Any]] = []
        for pid in list(window_pids)[:MAX_PROCESSES_IN_SNAPSHOT]:
            try:
                proc = psutil.Process(pid)
                summaries.append({
                    "pid": pid,
                    "process_name": (proc.name() or "").casefold(),
                    "executable_path": proc.exe(),
                    "start_time": proc.create_time(),
                    "parent_pid": proc.ppid() or None,
                    "foreground_relation": any(
                        w["pid"] == pid and w.get("foreground") for w in windows),
                    "cpu_percent": proc.cpu_percent(interval=None),
                    "memory_rss": proc.memory_info().rss,
                })
            except Exception:  # noqa: BLE001 — processo pode sumir a qualquer momento
                continue
        return summaries

    def ui_tree(self, hwnd: int, max_depth: int = 3) -> dict:
        """UIA on-demand com cache curto implícito pelo chamador (§9)."""
        from app.desktop.uia import inspect_window

        depth = min(max_depth, 5)
        return inspect_window(hwnd, max_depth=depth)

    def clipboard_metadata(self) -> dict | None:
        if not self.config.clipboard_metadata_enabled:
            return None
        try:
            user32 = ctypes.windll.user32
            if not user32.OpenClipboard(0):
                return None
            try:
                fmt = user32.EnumClipboardFormats(0)
                length_bytes = user32.GlobalSize(user32.GetClipboardData(1)) if fmt == 1 else 0
                kinds = []
                while fmt:
                    kinds.append(fmt)
                    fmt = user32.EnumClipboardFormats(fmt)
                kind = {1: "text", 2: "image", 15: "files"}.get(kinds[0], f"format:{kinds[0]}") if kinds else "empty"
                # §11: apenas metadata; conteúdo NUNCA é lido aqui.
                sig = (kind, length_bytes)
                changed = self._previous_clipboard_sig is not None and sig != self._previous_clipboard_sig
                self._previous_clipboard_sig = sig
                return {
                    "type": kind,
                    "length": length_bytes if kind == "text" else None,
                    "changed": changed,
                    "updated_at": self.clock(),
                }
            finally:
                user32.CloseClipboard()
        except Exception:  # noqa: BLE001
            return None

    def recent_files(self) -> list[dict]:
        if not self.config.recent_files_enabled:
            return []
        cached_at, cached = self._recent_files_cache
        now = self.clock()
        if cached and now - cached_at < 20.0:
            return cached
        home = Path.home()
        entries: list[dict] = []
        for root_name in RECENT_FILE_ROOTS:
            root = home / root_name
            try:
                if not root.is_dir():
                    continue
                for child in root.iterdir():
                    try:
                        if child.is_file():
                            stat = child.stat()
                            entries.append({
                                "path": str(child),
                                "name": child.name,
                                "mtime": stat.st_mtime,
                                "size": stat.st_size,
                                "root": root_name.casefold(),
                            })
                    except OSError:
                        continue
            except OSError:
                continue
        entries.sort(key=lambda item: item["mtime"], reverse=True)
        entries = entries[:MAX_RECENT_FILES]
        self._recent_files_cache = (now, entries)
        return entries

    # -------------------------------------------------------------- eventos

    def emit_diff_events(self, snapshot: dict[str, Any]) -> list[str]:
        """Compara com o snapshot anterior e publica eventos normalizados."""
        emitted: list[str] = []
        current: dict[int, dict] = {w["hwnd"]: w for w in snapshot.get("windows", [])}
        prev = self._previous_windows
        current_processes = {
            int(proc["pid"]): proc for proc in snapshot.get("processes", [])
            if proc.get("pid") is not None
        }
        previous_processes = self._previous_processes

        for hwnd, win in current.items():
            if hwnd not in prev:
                emitted.append(self._emit("WINDOW_OPENED", hwnd=hwnd, title=win["title"],
                                          process=win["process"]))
                if not any(old.get("pid") == win.get("pid") for old in prev.values()):
                    emitted.append(self._emit("APPLICATION_LAUNCHED", hwnd=hwnd,
                                              pid=win.get("pid"), process=win["process"]))
                if win.get("window_class") == "#32770":
                    emitted.append(self._emit("DIALOG_DETECTED", hwnd=hwnd,
                                              process=win["process"]))
        for hwnd, win in prev.items():
            if hwnd not in current:
                emitted.append(self._emit("WINDOW_CLOSED", hwnd=hwnd, title=win.get("title", ""),
                                          process=win.get("process", "")))
                if not any(opened.get("pid") == win.get("pid")
                           for opened in current.values()):
                    emitted.append(self._emit("APPLICATION_CLOSED", hwnd=hwnd,
                                              pid=win.get("pid"), process=win.get("process", "")))
            elif win["title"] != current[hwnd]["title"]:
                pass  # mudança de título é ruído frequente: sem evento (anti-storm)
        fg_now = snapshot.get("foreground_window") or {}
        fg_prev = next((w for w in prev.values() if w.get("foreground")), None)
        if fg_now and (not fg_prev or fg_prev["hwnd"] != fg_now.get("hwnd")):
            emitted.append(self._emit("WINDOW_FOREGROUND_CHANGED", hwnd=fg_now.get("hwnd"),
                                      title=fg_now.get("title"), process=fg_now.get("process")))
        clip = snapshot.get("clipboard")
        if clip and clip.get("changed"):
            emitted.append(self._emit("CLIPBOARD_CHANGED", type=clip.get("type"),
                                      length=clip.get("length")))

        for pid, proc in current_processes.items():
            if pid not in previous_processes:
                emitted.append(self._emit("PROCESS_STARTED", pid=pid,
                                          process=proc.get("process_name", "")))
        for pid, proc in previous_processes.items():
            if pid not in current_processes:
                emitted.append(self._emit("PROCESS_STOPPED", pid=pid,
                                          process=proc.get("process_name", "")))

        current_files = {
            str(item.get("path") or ""): item for item in snapshot.get("recent_files", [])
            if item.get("path")
        }
        if self._previous_files:
            for path, item in current_files.items():
                previous = self._previous_files.get(path)
                if previous is None:
                    emitted.append(self._emit("FILE_CREATED", path=path,
                                              root=item.get("root")))
                elif (item.get("mtime"), item.get("size")) != \
                        (previous.get("mtime"), previous.get("size")):
                    emitted.append(self._emit("FILE_MODIFIED", path=path,
                                              root=item.get("root")))
        self._previous_windows = current
        self._previous_processes = current_processes
        self._previous_files = current_files
        return [event for event in emitted if event]

    def _emit(self, name: str, **payload) -> str | None:
        """Enfileira evento normalizado com debounce (§16 anti-storm).

        A publicação real acontece no event loop (flush_pending_events),
        porque o EventBus existente é async-only e os subscribers têm prazo
        blindado — percepção em thread nunca publica direto.
        """
        identity = (payload.get("hwnd") or payload.get("pid") or
                    payload.get("path") or payload.get("type") or "")
        key = f"{name}:{identity}"
        now = self.clock()
        last = self._debounce.get(key)
        if last is not None and now - last < 0.4:
            return None
        self._debounce[key] = now
        if len(self._debounce) > 512:
            stale_cut = now - 60
            self._debounce = {k: v for k, v in self._debounce.items() if v > stale_cut}
        self._pending_events.append((name, payload))
        return getattr(EventType, f"COMPUTER_{name}", getattr(EventType, name))

    async def flush_pending_events(self, limit: int = 32) -> int:
        """Publica eventos pendentes no EventBus (chamado do event loop)."""
        flushed = 0
        while self._pending_events and flushed < limit:
            name, payload = self._pending_events.pop(0)
            member = getattr(EventType, f"COMPUTER_{name}", None) or getattr(EventType, name)
            try:
                await self.event_bus.publish(member, source="computer_perception", **payload)
            except Exception:  # noqa: BLE001
                logger.warning("computer_event_publish_failed event=%s", name)
                continue
            flushed += 1
        if len(self._pending_events) > 128:  # §81 max queues
            del self._pending_events[:-64]
        return flushed

    # ----------------------------------------------------------------- loop

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="kazumi-computer-perception")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        interval = max(self.config.refresh_interval_seconds, 1.0)
        while True:
            try:
                snapshot = await asyncio.to_thread(self.snapshot)
                if self._snapshot_consumer is not None:
                    self._snapshot_consumer(snapshot)
                self.emit_diff_events(snapshot)
                await self.flush_pending_events()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning("computer_perception_tick_failed type=%s", type(error).__name__)
                self.metrics["perception_failure"] += 1
                try:
                    now = self.clock()
                    failure_key = "PERCEPTION_FAILURE:tick"
                    last_failure = self._debounce.get(failure_key)
                    if last_failure is None or now - last_failure >= 30.0:
                        self._debounce[failure_key] = now
                        await self.event_bus.publish(
                            EventType.COMPUTER_PERCEPTION_FAILURE,
                            source="computer_perception",
                            stage="snapshot",
                            error_type=type(error).__name__,
                        )
                except Exception:  # noqa: BLE001 — telemetria nunca derruba o loop
                    pass
            await asyncio.sleep(interval)

    # --------------------------------------------------- fallbacks visuais

    def capture_window_image(self, hwnd: int):
        """Visão SOB DEMANDA (§14): janela específica, nunca desktop inteiro."""
        from app.operator.vision_capture import capture_window  # fallback real já existente

        return capture_window(hwnd)

    def ocr_region(self, image, region: tuple[int, int, int, int] | None = None) -> dict:
        """OCR como ÚLTIMO recurso (§15); resultado marcado com fonte/confiança."""
        from app.operator.vision_ocr import run_ocr

        result = run_ocr(image, region)
        if isinstance(result, dict):
            result.setdefault("source", "OCR")
        return result
