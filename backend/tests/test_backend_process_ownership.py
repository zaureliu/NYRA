from __future__ import annotations

import os
import threading

from app.core.process_ownership import ParentProcessWatch, owned_parent_pid


def test_owned_parent_pid_requires_explicit_owned_environment(monkeypatch):
    monkeypatch.delenv("KAZUMI_BACKEND_OWNED", raising=False)
    monkeypatch.setenv("KAZUMI_PARENT_PID", "42")
    assert owned_parent_pid() is None
    monkeypatch.setenv("KAZUMI_BACKEND_OWNED", "1")
    assert owned_parent_pid() == 42


def test_parent_disappearance_requests_shutdown_once():
    lost = threading.Event()
    calls = []

    def probe(_pid: int, _created_at: float) -> bool:
        calls.append(True)
        return len(calls) < 2

    watch = ParentProcessWatch(os.getpid(), lost.set, interval_seconds=0.05, probe=probe)
    watch.start()
    assert lost.wait(timeout=1)
    watch.stop()
    assert len(calls) == 2


def test_stopping_parent_watch_preserves_live_parent():
    lost = threading.Event()
    watch = ParentProcessWatch(os.getpid(), lost.set, interval_seconds=0.05, probe=lambda *_: True)
    watch.start()
    watch.stop()
    assert not lost.is_set()
