"""Lifecycle guard for a backend process owned by the packaged desktop.

The parent PID is supplied only by the Tauri launcher.  A low-frequency
watch verifies both PID existence and its original creation time, so PID reuse
cannot make an orphaned backend live indefinitely.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

import psutil


logger = logging.getLogger("nyra.lifecycle")


class ParentProcessWatch:
    def __init__(
        self,
        parent_pid: int,
        on_parent_lost: Callable[[], None],
        *,
        interval_seconds: float = 2.0,
        probe: Callable[[int, float], bool] | None = None,
    ) -> None:
        self.parent_pid = parent_pid
        self.on_parent_lost = on_parent_lost
        self.interval_seconds = max(0.05, interval_seconds)
        parent = psutil.Process(parent_pid)
        self.parent_created_at = float(parent.create_time())
        self._probe = probe or self._process_matches
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _process_matches(pid: int, created_at: float) -> bool:
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE and abs(
                float(process.create_time()) - created_at
            ) < 0.01
        except (psutil.Error, OSError):
            return False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="nyra-parent-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.interval_seconds + 0.5))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if self._probe(self.parent_pid, self.parent_created_at):
                continue
            logger.warning("backend_parent_disappeared parent_pid=%s", self.parent_pid)
            self.on_parent_lost()
            return


def owned_parent_pid() -> int | None:
    if os.environ.get("NYRA_BACKEND_OWNED") != "1":
        return None
    try:
        value = int(os.environ.get("NYRA_PARENT_PID", ""))
        return value if value > 0 and value != os.getpid() else None
    except ValueError:
        return None
