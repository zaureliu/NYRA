"""Persistent Job Manager (spec Parte F §115-§138).

Long tasks never hold a tool call hostage (§116). Jobs are REAL detached OS
processes with: start/status/list/logs/cancel/pause/resume (§118-§124), full
state machine (§125/§126), progress extracted ONLY from real output (§127/§128),
pid+create_time identity (§129), log rotation (§130), persistence + reattach
after API restart (§131/§132), orphan detection (§133/§134), background
execution outside the shell timeout path (§135) and per-resource locks (§136).
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT, LOG_ROOT
from app.tools.redaction import redact_secrets

_PROGRESS_PATTERNS = [
    re.compile(r"(?i)\b(\d{1,3}(?:\.\d+)?)%"),
    re.compile(r"(?i)(?:progress|progresso)[^\d]{0,12}(\d{1,3})"),
]

_LOG_MAX_BYTES = 2 * 1024 * 1024  # §130 rotation threshold per file


class JobState(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


_TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class JobError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _psutil_identity(pid: int) -> tuple[str | None, float | None] | None:
    try:
        import psutil

        process = psutil.Process(pid)
        return process.name(), process.create_time()
    except Exception:  # noqa: BLE001
        return None


def _identity_alive(pid: int, create_time: float | None) -> bool:
    if not pid:
        return False
    identity = _psutil_identity(pid)
    if identity is None:
        return False
    if create_time and abs(identity[1] - float(create_time)) > 0.5:
        return False  # PID reuse (§129)
    return True


def _kill_tree(pid: int) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _suspend_resume(pid: int, *, suspend: bool) -> bool:
    try:
        import psutil

        process = psutil.Process(pid)
        if suspend:
            process.suspend()
        else:
            process.resume()
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_log_tail(path: Path, lines: int) -> str:
    try:
        if not path.exists():
            return ""
        text_lines = path.read_text("utf-8", errors="replace").splitlines()
        return "\n".join(text_lines[-max(5, min(lines, 400)):])
    except OSError:
        return ""


def _rotate_log(path: Path) -> None:
    """§130: keep one rotated generation when a job log grows too big."""
    try:
        if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            rotated.unlink(missing_ok=True)
            os.replace(path, rotated)
    except OSError:
        pass


def _public_record(record: dict) -> dict:
    keys = ("job_id", "name", "type", "state", "started_at", "finished_at",
            "exit_code", "pid", "create_time", "resource_key", "timeout_seconds",
            "command_preview")
    public = {key: record.get(key) for key in keys}
    public["progress"] = record.get("progress")
    runtime = None
    if record.get("started_at") and not record.get("finished_at"):
        runtime = round(time.time() - float(record["started_at"]), 1)
    public["runtime_seconds"] = runtime
    return public


class PersistentJobManager:
    def __init__(self, event_bus=None, *, database_path: Path | None = None,
                 max_jobs: int = 40) -> None:
        self.event_bus = event_bus
        self.max_jobs = max_jobs
        self.database_path = database_path or (DATA_ROOT / "nyra.db")
        self.log_dir = LOG_ROOT / "jobs"
        self._processes: dict[str, subprocess.Popen] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._monitor_task: asyncio.Task | None = None
        self._db_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        await self._initialize_store()
        await self.reattach()

    def start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="nyra-job-monitor")

    async def shutdown(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                await self.reconcile()
            except Exception:  # noqa: BLE001 - monitor must survive anything
                continue

    # ---------------------------------------------------------------------- start
    async def start(self, name: str, argv: list[str], *, job_type: str = "process",
                    working_directory: str = "", timeout_seconds: int | None = None,
                    resource_key: str = "") -> dict:
        if len(argv) < 1 or not str(argv[0]).strip():
            raise JobError("INVALID_ARGV", "argv requer um executável.")
        if not all(isinstance(part, str) and len(part) <= 2000 for part in argv):
            raise JobError("INVALID_ARGV", "argv inválido.")
        active = [job for job in (await self.list())["jobs"]
                  if job["state"] in {"QUEUED", "STARTING", "RUNNING", "WAITING", "PAUSED"}]
        if len(active) >= self.max_jobs:
            raise JobError("JOB_LIMIT", f"Limite de {self.max_jobs} jobs ativos.")
        lock_key = resource_key or f"job:{name.casefold()}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            raise JobError("RESOURCE_LOCKED", f"Recurso '{lock_key}' ocupado por outro job.")
        async with lock:
            job_id = f"job_{os.urandom(6).hex()}"
            out_path = self.log_dir / f"{job_id}.out.log"
            err_path = self.log_dir / f"{job_id}.err.log"
            record: dict[str, Any] = {
                "job_id": job_id,
                "name": name[:120],
                "type": job_type[:40],
                "state": JobState.STARTING.value,
                "started_at": time.time(),
                "finished_at": None,
                "exit_code": None,
                "pid": None,
                "create_time": None,
                "resource_key": lock_key,
                "timeout_seconds": timeout_seconds,
                "command_preview": redact_secrets(" ".join(argv))[:300],
            }
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                out_handle = open(out_path, "ab")  # noqa: SIM115 - lifetime é do processo
                err_handle = open(err_path, "ab")  # noqa: SIM115
                process = subprocess.Popen(  # noqa: S603 - argv direto, sem shell intermediário (§135)
                    argv,
                    cwd=str(Path(working_directory)) if working_directory else None,
                    stdin=subprocess.DEVNULL,
                    stdout=out_handle,
                    stderr=err_handle,
                    close_fds=True,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise JobError("SPAWN_FAILED", str(exc)[:200]) from exc
            identity = _psutil_identity(process.pid)
            record["pid"] = process.pid
            record["create_time"] = identity[1] if identity else None
            record["state"] = JobState.RUNNING.value
            self._processes[job_id] = process
            await self._save(record)
            await self._emit("JOB_STARTED", **_public_record(record))
            return {"success": True, "job": _public_record(record)}

    # -------------------------------------------------------------------- queries
    async def status(self, job_id: str) -> dict:
        record = await self._load(job_id)
        if not record:
            return {"success": False, "error_code": "JOB_NOT_FOUND"}
        await self.refresh_progress(record)
        return {"success": True, "job": _public_record(record)}

    async def list(self, include_terminal: bool = True) -> dict:
        rows = await self._query_all()
        jobs = []
        for row in rows:
            if not include_terminal and row["state"] in {state.value for state in _TERMINAL_STATES}:
                continue
            await self.refresh_progress(row)
            jobs.append(_public_record(row))
        return {"success": True, "jobs": jobs, "count": len(jobs)}

    async def logs(self, job_id: str, lines: int = 80) -> dict:
        record = await self._load(job_id)
        if not record:
            return {"success": False, "error_code": "JOB_NOT_FOUND"}
        tail_out = _read_log_tail(self.log_dir / f"{job_id}.out.log", lines)
        tail_err = _read_log_tail(self.log_dir / f"{job_id}.err.log", lines)
        return {"success": True,
                "stdout_tail": redact_secrets(tail_out),
                "stderr_tail": redact_secrets(tail_err)}

    # -------------------------------------------------------------------- control
    async def cancel(self, job_id: str) -> dict:
        record = await self._load(job_id)
        if not record:
            return {"success": False, "error_code": "JOB_NOT_FOUND"}
        state = JobState(record["state"])
        if state in _TERMINAL_STATES:
            return {"success": False, "error_code": "JOB_ALREADY_FINISHED", "state": state.value}
        killed = _kill_tree(int(record["pid"])) if record.get("pid") else False
        record["state"] = JobState.CANCELLED.value
        record["finished_at"] = time.time()
        self._processes.pop(job_id, None)
        await self._save(record)
        await self._emit("JOB_CANCELLED", **_public_record(record))
        return {"success": True, "killed_process": killed, "job": _public_record(record)}

    async def pause(self, job_id: str) -> dict:
        """§123: only when the process supports suspension."""
        record = await self._load(job_id)
        if not record or JobState(record["state"]) != JobState.RUNNING:
            return {"success": False, "error_code": "JOB_NOT_RUNNING"}
        paused = _suspend_resume(int(record["pid"]), suspend=True)
        if not paused:
            return {"success": False, "error_code": "PAUSE_UNSUPPORTED",
                    "message": "Processo não suporta suspensão."}
        record["state"] = JobState.PAUSED.value
        await self._save(record)
        return {"success": True, "job": _public_record(record)}

    async def resume(self, job_id: str) -> dict:
        record = await self._load(job_id)
        if not record or JobState(record["state"]) != JobState.PAUSED:
            return {"success": False, "error_code": "JOB_NOT_PAUSED"}
        resumed = _suspend_resume(int(record["pid"]), suspend=False)
        if not resumed:
            return {"success": False, "error_code": "RESUME_FAILED"}
        record["state"] = JobState.RUNNING.value
        await self._save(record)
        return {"success": True, "job": _public_record(record)}

    # ------------------------------------------------------ reconcile / reattach
    async def reattach(self) -> dict:
        """§132: after API restart, reconcile persisted RUNNING jobs."""
        reattached, orphaned = 0, 0
        for record in await self._query_all():
            state = JobState(record["state"])
            if state in _TERMINAL_STATES or state == JobState.UNKNOWN:
                continue
            alive = _identity_alive(int(record.get("pid") or 0), record.get("create_time"))
            if alive:
                reattached += 1
            else:
                orphaned += 1
                # §134: processo sumiu sem resultado.
                record["state"] = (JobState.FAILED.value if state in {JobState.RUNNING, JobState.STARTING}
                                   else JobState.UNKNOWN.value)
                record["finished_at"] = record.get("finished_at") or time.time()
                await self._save(record)
        return {"success": True, "reattached": reattached, "orphaned": orphaned}

    async def reconcile(self) -> None:
        for record in await self._query_all():
            state = JobState(record["state"])
            if state in _TERMINAL_STATES or state == JobState.PAUSED:
                continue
            alive = _identity_alive(int(record.get("pid") or 0), record.get("create_time"))
            if alive:
                await self.refresh_progress(record)
                _rotate_log(self.log_dir / f"{record['job_id']}.out.log")
                _rotate_log(self.log_dir / f"{record['job_id']}.err.log")
            else:
                exit_code = None
                process = self._processes.pop(record["job_id"], None)
                if process is not None and process.poll() is not None:
                    exit_code = process.returncode
                record["exit_code"] = exit_code if exit_code is not None else record.get("exit_code")
                succeeded = isinstance(exit_code, int) and exit_code == 0
                record["state"] = JobState.SUCCEEDED.value if succeeded else JobState.FAILED.value
                record["finished_at"] = time.time()
                await self._save(record)
                await self._emit("JOB_FINISHED", **_public_record(record))

    async def refresh_progress(self, record: dict) -> None:
        """§127/§128: progress ONLY from real output; otherwise null."""
        tail = _read_log_tail(self.log_dir / f"{record['job_id']}.out.log", 30)
        progress = None
        for pattern in _PROGRESS_PATTERNS:
            matches = pattern.findall(tail)
            if matches:
                candidate = float(matches[-1])
                if 0 <= candidate <= 100:
                    progress = round(candidate, 1)
                break
        record["progress"] = progress

    # --------------------------------------------------------------------- store
    async def _initialize_store(self) -> None:
        async with self._db_lock:
            def work() -> None:
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS operator_jobs (
                            job_id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            type TEXT,
                            state TEXT NOT NULL,
                            started_at REAL,
                            finished_at REAL,
                            exit_code INTEGER,
                            pid INTEGER,
                            create_time REAL,
                            resource_key TEXT,
                            timeout_seconds INTEGER,
                            command_preview TEXT
                        )
                        """
                    )
            await asyncio.to_thread(work)

    async def _save(self, record: dict) -> None:
        async with self._db_lock:
            def work() -> None:
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute(
                        """
                        INSERT INTO operator_jobs (job_id, name, type, state, started_at, finished_at,
                                                   exit_code, pid, create_time, resource_key,
                                                   timeout_seconds, command_preview)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            state=excluded.state,
                            finished_at=excluded.finished_at,
                            exit_code=excluded.exit_code
                        """,
                        (
                            record.get("job_id"), record.get("name"), record.get("type"),
                            record.get("state"), record.get("started_at"), record.get("finished_at"),
                            record.get("exit_code"), record.get("pid"), record.get("create_time"),
                            record.get("resource_key"), record.get("timeout_seconds"),
                            record.get("command_preview"),
                        ),
                    )
            await asyncio.to_thread(work)

    async def _load(self, job_id: str) -> dict | None:
        async with self._db_lock:
            def work() -> sqlite3.Row | None:
                with sqlite3.connect(self.database_path) as connection:
                    connection.row_factory = sqlite3.Row
                    row = connection.execute(
                        "SELECT * FROM operator_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    return dict(row) if row else None
            return await asyncio.to_thread(work)

    async def _query_all(self) -> list[dict]:
        async with self._db_lock:
            def work() -> list[dict]:
                with sqlite3.connect(self.database_path) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute(
                        "SELECT * FROM operator_jobs ORDER BY started_at DESC LIMIT 200"
                    ).fetchall()
                    return [dict(row) for row in rows]
            return await asyncio.to_thread(work)

    # ---------------------------------------------------------------------- misc
    async def _emit(self, event_type: str, **payload: Any) -> None:
        if self.event_bus is None:
            return
        from app.events import EventType

        try:
            event = EventType(event_type)
        except ValueError:
            event = EventType.ERROR
        try:
            await self.event_bus.publish(event, **payload)
        except Exception:  # noqa: BLE001 - events must never break jobs
            pass

    def lock_for(self, resource_key: str) -> asyncio.Lock:
        return self._locks.setdefault(resource_key, asyncio.Lock())
