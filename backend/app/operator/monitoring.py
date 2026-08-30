"""Persistent proactive monitoring backed by real read-only tools.

Monitor jobs are intentionally separate from process jobs: a monitor owns a
structured observation tool, a condition and a deadline.  No LLM text is ever
executed and no synthetic reading is accepted as evidence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.paths import DATA_ROOT
from app.core.turn import get_current_turn_id
from app.tools.models import RiskLevel
from app.tools.redaction import redact_secrets


logger = logging.getLogger("nyra.operator.monitoring")
_HISTORY_LIMIT = 12
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_PATH_PART = re.compile(r"([^.[\]]+)|\[(\d+)\]")
_MONITOR_PROMISE = re.compile(
    r"(?i)\b(?:vou|irei|estarei|passarei\s+a)\s+(?:continuar\s+)?(?:ficar\s+de\s+olho|"
    r"monitorar|acompanhar|monitorando|acompanhando|verificar\s+(?:periodicamente|depois|mais\s+tarde)|"
    r"te\s+avisar\s+quando)\b|"
    r"\b(?:continuarei|seguirei)\s+(?:monitorando|acompanhando|verificando)\b|"
    r"\bficarei\s+de\s+olho\b"
)
_MONITOR_CANCEL = re.compile(
    r"(?i)\b(?:para|pare|parar|cancela|cancele|cancelar|deixa|deixe)\b"
    r".{0,36}\b(?:monitorar|monitoramento|acompanhar|acompanhamento|monitor)\b|"
    r"\bn[aã]o\s+(?:monitore|acompanhe)\s+mais\b"
)


class MonitorStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ConditionOperator(StrEnum):
    LT = "LT"
    LTE = "LTE"
    GT = "GT"
    GTE = "GTE"
    EQ = "EQ"
    NE = "NE"
    CONTAINS = "CONTAINS"
    CHANGED = "CHANGED"
    TRUTHY = "TRUTHY"
    FALSY = "FALSY"


class MonitorCondition(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    operator: ConditionOperator
    target: Any = None


class MonitorReading(BaseModel):
    observed_at: float
    ok: bool
    value: Any = None
    error_code: str = ""
    summary: str = ""


class MonitorCreateRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=500)
    probe_tool: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    probe_params: dict[str, Any] = Field(default_factory=dict)
    condition: MonitorCondition
    interval_seconds: float = Field(default=30.0, ge=1.0, le=86400.0)
    duration_seconds: float = Field(default=3600.0, ge=2.0, le=2592000.0)
    significant_change: float | None = Field(default=None, ge=0)
    significant_change_percent: float = Field(default=10.0, ge=0, le=1000)
    notification_cooldown_seconds: float = Field(default=60.0, ge=0, le=86400)
    voice: bool = False


class MonitorJob(BaseModel):
    monitor_id: str = Field(default_factory=lambda: f"mon_{uuid4().hex}")
    objective: str
    probe_tool: str
    probe_params: dict[str, Any] = Field(default_factory=dict)
    condition: MonitorCondition
    interval_seconds: float
    duration_seconds: float
    significant_change: float | None = None
    significant_change_percent: float = 10.0
    notification_cooldown_seconds: float = 60.0
    voice: bool = False
    status: MonitorStatus = MonitorStatus.ACTIVE
    created_at: float = Field(default_factory=time.time)
    deadline_at: float
    next_run_at: float
    updated_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    last_reading: MonitorReading | None = None
    history: list[MonitorReading] = Field(default_factory=list)
    sample_count: int = 0
    consecutive_errors: int = 0
    max_consecutive_errors: int = 3
    last_error_signature: str = ""
    last_notified_at: float | None = None
    last_notified_value: Any = None
    completion_reason: str = ""
    final_summary: str = ""
    source_turn_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "objective": self.objective,
            "probe_tool": self.probe_tool,
            "condition": self.condition.model_dump(mode="json"),
            "interval_seconds": self.interval_seconds,
            "duration_seconds": self.duration_seconds,
            "status": self.status.value,
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
            "next_run_at": self.next_run_at if self.status == MonitorStatus.ACTIVE else None,
            "finished_at": self.finished_at,
            "last_reading": self.last_reading.model_dump(mode="json") if self.last_reading else None,
            "history": [item.model_dump(mode="json") for item in self.history],
            "sample_count": self.sample_count,
            "consecutive_errors": self.consecutive_errors,
            "completion_reason": self.completion_reason,
            "final_summary": self.final_summary,
            "voice": self.voice,
            "source_turn_id": self.source_turn_id,
        }


class MonitorJobError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def contains_monitor_promise(text: str) -> bool:
    return bool(_MONITOR_PROMISE.search(text or ""))


def is_monitor_cancel_request(text: str) -> bool:
    return bool(_MONITOR_CANCEL.search(text or ""))


def enforce_monitor_promise(text: str, *, job_created: bool) -> str:
    """Fail closed when prose promises a background action that does not exist."""
    if not contains_monitor_promise(text) or job_created:
        return text
    return (
        "Não consegui criar um MonitorJob real para esse acompanhamento. "
        "Portanto, não há monitoramento ativo; a falha precisa ser corrigida antes de eu prometer acompanhar isso."
    )


class MonitorJobManager:
    def __init__(self, registry, event_bus, *, database_path: Path | None = None,
                 clock=time.time) -> None:
        self.registry = registry
        self.event_bus = event_bus
        self.database_path = database_path or (DATA_ROOT / "nyra.db")
        self.clock = clock
        self._jobs: dict[str, MonitorJob] = {}
        self._runner: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._db_lock = asyncio.Lock()
        self._job_locks: dict[str, asyncio.Lock] = {}

    # --------------------------------------------------------------- lifecycle
    async def initialize(self) -> dict[str, Any]:
        await self._initialize_store()
        recovered = await self._load_all()
        self._jobs = {job.monitor_id: job for job in recovered}
        active = 0
        now = self.clock()
        for job in self._jobs.values():
            if job.status == MonitorStatus.ACTIVE:
                job.next_run_at = min(job.next_run_at, now)
                await self._save(job)
                active += 1
        return {"success": True, "recovered": active}

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run_loop(), name="nyra-monitor-jobs")

    async def shutdown(self) -> None:
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        self._runner = None

    # ------------------------------------------------------------------ create
    async def create(self, request: MonitorCreateRequest, *,
                     source_turn_id: str | None = None) -> dict[str, Any]:
        self._ensure_probe_allowed(request.probe_tool, request.probe_params)
        now = self.clock()
        safe_objective = redact_secrets(" ".join(request.objective.split()))[:500]
        safe_params = _safe_json_value(request.probe_params, max_characters=6000)
        job = MonitorJob(
            objective=safe_objective,
            probe_tool=request.probe_tool,
            probe_params=safe_params if isinstance(safe_params, dict) else {},
            condition=request.condition,
            interval_seconds=request.interval_seconds,
            duration_seconds=request.duration_seconds,
            significant_change=request.significant_change,
            significant_change_percent=request.significant_change_percent,
            notification_cooldown_seconds=request.notification_cooldown_seconds,
            voice=request.voice,
            created_at=now,
            deadline_at=now + request.duration_seconds,
            next_run_at=now + request.interval_seconds,
            updated_at=now,
            source_turn_id=source_turn_id or get_current_turn_id(),
        )
        # A first real invocation proves that the configured tool and path exist.
        try:
            reading = await self._observe(job)
        except MonitorJobError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MonitorJobError(
                "MONITOR_INITIAL_PROBE_FAILED",
                f"A leitura inicial de {request.probe_tool} falhou: {type(exc).__name__}",
            ) from exc

        self._record_reading(job, reading)
        job.last_notified_value = reading.value if reading.ok else None
        self._jobs[job.monitor_id] = job
        await self._save(job)
        await self._emit("MONITOR_JOB_CREATED", **job.public_dict())

        if reading.ok and self._condition_met(job, reading.value, previous=None):
            await self._finish(job, MonitorStatus.COMPLETED, "CONDITION_MET")
        elif not reading.ok:
            await self._notify_error_once(job, reading)
        self._wake.set()
        return {
            "success": True,
            "effect_verified": True,
            "monitor": job.public_dict(),
        }

    # ---------------------------------------------------------------- queries
    async def list(self, *, include_terminal: bool = True, limit: int = 100) -> dict[str, Any]:
        jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        if not include_terminal:
            jobs = [job for job in jobs if job.status == MonitorStatus.ACTIVE]
        values = [job.public_dict() for job in jobs[:max(1, min(limit, 200))]]
        return {"success": True, "monitors": values, "count": len(values)}

    async def status(self, monitor_id: str) -> dict[str, Any]:
        job = self._jobs.get(monitor_id)
        if job is None:
            return {"success": False, "error_code": "MONITOR_NOT_FOUND"}
        return {"success": True, "monitor": job.public_dict()}

    def has_job_for_turn(self, turn_id: str | None) -> bool:
        return bool(turn_id) and any(job.source_turn_id == turn_id for job in self._jobs.values())

    # ---------------------------------------------------------------- control
    async def cancel(self, monitor_id: str, *, notify: bool = True) -> dict[str, Any]:
        job = self._jobs.get(monitor_id)
        if job is None:
            return {"success": False, "error_code": "MONITOR_NOT_FOUND"}
        if job.status != MonitorStatus.ACTIVE:
            return {
                "success": False,
                "error_code": "MONITOR_ALREADY_FINISHED",
                "status": job.status.value,
            }
        await self._finish(job, MonitorStatus.CANCELLED, "OPERATOR_CANCELLED", notify=notify)
        return {"success": True, "monitor": job.public_dict()}

    async def cancel_from_text(self, text: str) -> dict[str, Any]:
        active = [job for job in self._jobs.values() if job.status == MonitorStatus.ACTIVE]
        if not active:
            return {"success": False, "message": "Não há MonitorJob ativo para cancelar."}
        normalized = _normalize(text)
        explicit = next((job for job in active if job.monitor_id.casefold() in normalized), None)
        if explicit is not None:
            selected = explicit
        elif re.search(r"\b(?:isso|esse|este|o\s+monitoramento)\b", normalized) or len(active) == 1:
            selected = max(active, key=lambda item: item.created_at)
        else:
            words = {word for word in re.findall(r"[a-z0-9_]{3,}", normalized)
                     if word not in {"para", "pare", "parar", "monitorar", "monitoramento", "acompanhar"}}
            ranked = sorted(
                ((len(words & set(re.findall(r"[a-z0-9_]{3,}", _normalize(job.objective)))), job)
                 for job in active),
                key=lambda item: (item[0], item[1].created_at),
                reverse=True,
            )
            if not ranked or ranked[0][0] == 0:
                return {
                    "success": False,
                    "message": f"Há {len(active)} monitoramentos ativos; indique o objetivo ou o MonitorJob.",
                }
            selected = ranked[0][1]
        result = await self.cancel(selected.monitor_id, notify=False)
        monitor = result.get("monitor") or {}
        return {
            **result,
            "message": str(monitor.get("final_summary") or "Monitoramento cancelado."),
        }

    # --------------------------------------------------------------- scheduler
    async def _run_loop(self) -> None:
        while True:
            now = self.clock()
            active = [job for job in self._jobs.values() if job.status == MonitorStatus.ACTIVE]
            due = [job for job in active if job.next_run_at <= now or job.deadline_at <= now]
            if due:
                await asyncio.gather(*(self._tick(job.monitor_id) for job in due))
                continue
            deadlines = [min(job.next_run_at, job.deadline_at) for job in active]
            timeout = max(0.02, min(1.0, min(deadlines) - now)) if deadlines else 1.0
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    async def _tick(self, monitor_id: str) -> None:
        lock = self._job_locks.setdefault(monitor_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            job = self._jobs.get(monitor_id)
            if job is None or job.status != MonitorStatus.ACTIVE:
                return
            now = self.clock()
            if now >= job.deadline_at:
                await self._finish(job, MonitorStatus.COMPLETED, "TIMEOUT")
                return
            previous = job.last_reading.value if job.last_reading and job.last_reading.ok else None
            try:
                self._ensure_probe_allowed(job.probe_tool, job.probe_params)
                reading = await self._observe(job)
            except MonitorJobError as exc:
                reading = MonitorReading(
                    observed_at=self.clock(), ok=False, error_code=exc.code,
                    summary=redact_secrets(str(exc))[:240],
                )
            except Exception as exc:  # noqa: BLE001
                reading = MonitorReading(
                    observed_at=self.clock(), ok=False,
                    error_code="MONITOR_PROBE_EXCEPTION",
                    summary=f"{type(exc).__name__}: {redact_secrets(str(exc))[:180]}",
                )
            self._record_reading(job, reading)
            job.next_run_at = self.clock() + job.interval_seconds
            if not reading.ok:
                await self._notify_error_once(job, reading)
                if job.consecutive_errors >= job.max_consecutive_errors:
                    await self._finish(job, MonitorStatus.FAILED, "PROBE_ERRORS")
                    return
                await self._save(job)
                return

            job.consecutive_errors = 0
            job.last_error_signature = ""
            if self._condition_met(job, reading.value, previous=previous):
                await self._finish(job, MonitorStatus.COMPLETED, "CONDITION_MET")
                return
            if self._significant_change(job, reading.value):
                await self._notify_change(job, previous=job.last_notified_value, current=reading.value)
                job.last_notified_value = reading.value
                job.last_notified_at = self.clock()
            await self._save(job)
            await self._emit(
                "MONITOR_JOB_READING", monitor_id=job.monitor_id,
                observed_at=reading.observed_at, value=reading.value,
                sample_count=job.sample_count,
            )

    async def _observe(self, job: MonitorJob) -> MonitorReading:
        try:
            result = await self.registry.execute(job.probe_tool, job.probe_params, exposure="internal")
        except (KeyError, PermissionError, ValueError) as exc:
            raise MonitorJobError("MONITOR_PROBE_INVALID", redact_secrets(str(exc))[:240]) from exc
        data = result.data if isinstance(result.data, dict) else {}
        if not result.ok:
            code = str(data.get("error_code") or data.get("error") or "MONITOR_PROBE_FAILED")[:80]
            message = str(data.get("message") or data.get("stderr") or "A tool de observação falhou.")
            return MonitorReading(
                observed_at=self.clock(), ok=False, error_code=code,
                summary=redact_secrets(" ".join(message.split()))[:240],
            )
        found, value = _extract_path(data, job.condition.path)
        if not found:
            raise MonitorJobError(
                "MONITOR_CONDITION_PATH_NOT_FOUND",
                f"O campo '{job.condition.path}' não existe no resultado real de {job.probe_tool}.",
            )
        safe_value = _safe_json_value(value, max_characters=1800)
        return MonitorReading(
            observed_at=self.clock(), ok=True, value=safe_value,
            summary=f"{job.condition.path}={_display_value(safe_value)}",
        )

    def _record_reading(self, job: MonitorJob, reading: MonitorReading) -> None:
        job.last_reading = reading
        job.history = [*job.history, reading][-_HISTORY_LIMIT:]
        job.sample_count += 1
        job.updated_at = self.clock()
        if reading.ok:
            job.consecutive_errors = 0
        else:
            job.consecutive_errors += 1

    def _condition_met(self, job: MonitorJob, value: Any, *, previous: Any) -> bool:
        operator = job.condition.operator
        target = job.condition.target
        try:
            if operator == ConditionOperator.LT:
                return _number(value) < _number(target)
            if operator == ConditionOperator.LTE:
                return _number(value) <= _number(target)
            if operator == ConditionOperator.GT:
                return _number(value) > _number(target)
            if operator == ConditionOperator.GTE:
                return _number(value) >= _number(target)
            if operator == ConditionOperator.EQ:
                return value == target
            if operator == ConditionOperator.NE:
                return value != target
            if operator == ConditionOperator.CONTAINS:
                return str(target).casefold() in str(value).casefold()
            if operator == ConditionOperator.CHANGED:
                return previous is not None and value != previous
            if operator == ConditionOperator.TRUTHY:
                return bool(value)
            if operator == ConditionOperator.FALSY:
                return not bool(value)
        except (TypeError, ValueError):
            return False
        return False

    def _significant_change(self, job: MonitorJob, current: Any) -> bool:
        previous = job.last_notified_value
        if previous is None or current == previous:
            return False
        try:
            delta = abs(_number(current) - _number(previous))
            if job.significant_change is not None:
                relevant = delta >= job.significant_change
            else:
                baseline = abs(_number(previous))
                percent = math.inf if baseline == 0 and delta else (delta / baseline * 100 if baseline else 0)
                relevant = percent >= job.significant_change_percent
        except (TypeError, ValueError):
            relevant = True
        if not relevant:
            return False
        if job.last_notified_at is None:
            return True
        return self.clock() - job.last_notified_at >= job.notification_cooldown_seconds

    # ------------------------------------------------------------- notifications
    async def _notify_change(self, job: MonitorJob, *, previous: Any, current: Any) -> None:
        message = (
            f"Monitoramento: houve mudança relevante em {job.objective}: "
            f"{_display_value(previous)} → {_display_value(current)}."
        )
        await self._notification(job, message, kind="CHANGE", severity="info", voice=False)
        await self._emit(
            "MONITOR_JOB_CHANGED", monitor_id=job.monitor_id,
            objective=job.objective, previous=previous, current=current,
        )

    async def _notify_error_once(self, job: MonitorJob, reading: MonitorReading) -> None:
        signature = f"{reading.error_code}:{reading.summary}"
        if signature == job.last_error_signature:
            return
        job.last_error_signature = signature
        await self._notification(
            job,
            f"Monitoramento: erro ao verificar {job.objective}: {reading.summary or reading.error_code}.",
            kind="ERROR", severity="warning", voice=False,
        )

    async def _finish(self, job: MonitorJob, status: MonitorStatus, reason: str,
                      *, notify: bool = True) -> None:
        if job.status != MonitorStatus.ACTIVE:
            return
        job.status = status
        job.completion_reason = reason
        job.finished_at = self.clock()
        job.updated_at = job.finished_at
        last = job.last_reading.value if job.last_reading and job.last_reading.ok else "sem leitura válida"
        if reason == "CONDITION_MET":
            summary = (
                f"Monitoramento concluído: a condição de {job.objective} foi atingida. "
                f"Última leitura: {_display_value(last)}; {job.sample_count} amostra(s)."
            )
            kind, severity = "CONDITION_MET", "success"
        elif reason == "TIMEOUT":
            summary = (
                f"Monitoramento encerrado pelo prazo: {job.objective}. A condição não foi atingida. "
                f"Última leitura: {_display_value(last)}; {job.sample_count} amostra(s)."
            )
            kind, severity = "TIMEOUT", "info"
        elif reason == "OPERATOR_CANCELLED":
            summary = (
                f"Monitoramento cancelado: {job.objective}. "
                f"Última leitura: {_display_value(last)}; {job.sample_count} amostra(s)."
            )
            kind, severity = "CANCELLED", "info"
        else:
            detail = job.last_reading.summary if job.last_reading else "sem detalhe"
            summary = (
                f"Monitoramento falhou após {job.consecutive_errors} erro(s): {job.objective}. "
                f"Último erro: {detail}."
            )
            kind, severity = "FAILED", "error"
        job.final_summary = redact_secrets(summary)[:1000]
        await self._save(job)
        event_name = {
            MonitorStatus.COMPLETED: "MONITOR_JOB_COMPLETED",
            MonitorStatus.FAILED: "MONITOR_JOB_FAILED",
            MonitorStatus.CANCELLED: "MONITOR_JOB_CANCELLED",
        }[status]
        await self._emit(event_name, **job.public_dict())
        if notify:
            await self._notification(
                job, job.final_summary, kind=kind, severity=severity,
                voice=bool(job.voice and reason in {"CONDITION_MET", "TIMEOUT", "PROBE_ERRORS"}),
            )

    async def _notification(self, job: MonitorJob, message: str, *, kind: str,
                            severity: str, voice: bool) -> None:
        await self._emit(
            "MONITOR_NOTIFICATION", monitor_id=job.monitor_id,
            objective=job.objective, message=redact_secrets(message)[:1000],
            kind=kind, severity=severity, status=job.status.value, voice=voice,
            source="monitor_job",
        )

    # -------------------------------------------------------------------- store
    async def _initialize_store(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._db_lock:
            def work() -> None:
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS monitor_jobs (
                            monitor_id TEXT PRIMARY KEY,
                            status TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            payload_json TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_monitor_jobs_status_due "
                        "ON monitor_jobs(status, updated_at)"
                    )
            await asyncio.to_thread(work)

    async def _save(self, job: MonitorJob) -> None:
        payload = redact_secrets(job.model_dump_json())
        async with self._db_lock:
            def work() -> None:
                with sqlite3.connect(self.database_path) as connection:
                    connection.execute(
                        """
                        INSERT INTO monitor_jobs (monitor_id, status, created_at, updated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(monitor_id) DO UPDATE SET
                            status=excluded.status,
                            updated_at=excluded.updated_at,
                            payload_json=excluded.payload_json
                        """,
                        (job.monitor_id, job.status.value, job.created_at, job.updated_at, payload),
                    )
            await asyncio.to_thread(work)

    async def _load_all(self) -> list[MonitorJob]:
        async with self._db_lock:
            def work() -> list[str]:
                with sqlite3.connect(self.database_path) as connection:
                    active = connection.execute(
                        "SELECT payload_json FROM monitor_jobs WHERE status = 'ACTIVE' "
                        "ORDER BY created_at DESC"
                    ).fetchall()
                    terminal = connection.execute(
                        "SELECT payload_json FROM monitor_jobs WHERE status != 'ACTIVE' "
                        "ORDER BY created_at DESC LIMIT 200"
                    ).fetchall()
                    return [str(row[0]) for row in [*active, *terminal]]
            payloads = await asyncio.to_thread(work)
        jobs: list[MonitorJob] = []
        for payload in payloads:
            try:
                jobs.append(MonitorJob.model_validate_json(payload))
            except Exception:  # noqa: BLE001 - a damaged row cannot stop recovery
                logger.warning("monitor_job_row_invalid")
        return jobs

    # --------------------------------------------------------------------- misc
    def _ensure_probe_allowed(self, tool: str, params: dict[str, Any]) -> None:
        if tool.startswith("monitor_") or tool in {"task_create", "job_start", "workflow_run"}:
            raise MonitorJobError("MONITOR_PROBE_NOT_ALLOWED", "A tool escolhida não é uma observação.")
        preflight = self.registry.preflight(tool, params)
        if str(preflight.get("risk_level") or "").upper() != RiskLevel.READ_ONLY.value:
            raise MonitorJobError(
                "MONITOR_PROBE_NOT_READ_ONLY",
                f"Monitoramentos aceitam somente tools READ_ONLY; {tool} foi classificada como "
                f"{preflight.get('risk_level') or 'UNKNOWN'}.",
            )

    async def _emit(self, event_name: str, **payload: Any) -> None:
        from app.events import EventType

        try:
            event = EventType(event_name)
        except ValueError:
            logger.error("monitor_event_not_registered", extra={"event_name": event_name})
            return
        await self.event_bus.publish(event, **payload)


class ProactiveMonitorNotifications:
    """Adds optional voice to MonitorJob chat notifications."""

    def __init__(self, settings, event_bus, speech_queue, provider_getter,
                 voice_processor=None) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.speech_queue = speech_queue
        self.provider_getter = provider_getter
        self.voice_processor = voice_processor
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await self.event_bus.subscribe(self.handle_event)

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.handle_event)
        for task in tuple(self._tasks):
            task.cancel()

    async def handle_event(self, event) -> None:
        from app.events import EventType

        if event.type != EventType.MONITOR_NOTIFICATION or not event.payload.get("voice"):
            return
        task = asyncio.create_task(self._speak(event.payload), name="nyra-monitor-notification-voice")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _speak(self, payload: dict[str, Any]) -> None:
        if bool(getattr(self.settings, "network_quiet_mode", False)):
            return
        provider = self.provider_getter()
        if not await provider.health():
            return
        from app.speech.prosody import ProsodyProcessor
        from app.speech.queue import SpeechPriority

        message = str(payload.get("message") or "")[:1000]
        if not message:
            return
        state = "concerned" if payload.get("severity") in {"warning", "error"} else "neutral"
        prepared = ProsodyProcessor().prepare(message, provider=provider.name)
        priority = SpeechPriority.WARNING if state == "concerned" else SpeechPriority.INFORMATIONAL
        try:
            from app.events import EventType

            await self.event_bus.publish(
                EventType.TTS_STARTED, state=state, proactive=True, source="monitor_job"
            )
            output = await self.speech_queue.synthesize(
                provider, prepared.speech_text, state, priority
            )
            if self.voice_processor and self.voice_processor.config.enabled:
                output = await self.voice_processor.process(output, state)
            await self.event_bus.publish(
                EventType.TTS_FINISHED, state=state,
                audio_url=f"/api/audio/{Path(output).name}",
                proactive=True, source="monitor_job",
            )
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("monitor_notification_tts_failed", extra={"error_type": type(exc).__name__})


def _extract_path(data: Any, path: str) -> tuple[bool, Any]:
    current = data
    parts = list(_PATH_PART.finditer(path))
    if not parts:
        return False, None
    cursor = 0
    for part_index, match in enumerate(parts):
        separator = path[cursor:match.start()]
        key, index = match.groups()
        expected = "" if part_index == 0 or index is not None else "."
        if separator != expected:
            return False, None
        cursor = match.end()
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
        else:
            position = int(index)
            if not isinstance(current, list) or position >= len(current):
                return False, None
            current = current[position]
    if cursor != len(path):
        return False, None
    return True, current


def _safe_json_value(value: Any, *, max_characters: int) -> Any:
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = json.dumps(str(value), ensure_ascii=False)
    redacted = redact_secrets(serialized)
    if len(redacted) > max_characters:
        return redacted[:max_characters] + "…"
    try:
        return json.loads(redacted)
    except ValueError:
        return redacted


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise TypeError("valor não numérico")
    return float(value)


def _display_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:180]
    return str(value)[:180]


def _normalize(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))
