"""Autonomous Task Planner V2 (spec Parte G §139-§157).

The Agent Controller stays the single brain (§6); this manager is the
capability that lets LONG tasks survive beyond one turn/tool call:
    §141  task model with verification_plan and deadline;
    §142  full state machine incl. WAITING_FOR_USER / WAITING_FOR_JOB /
          VERIFYING / RECOVERING;
    §143  steps with action/tool/resource/dependencies/status/verification;
    §146/§147  confirmations only when policy demands (tools keep their own
          approval gates);
    §148-§150  persistence + safe resumption; destructive steps NEVER
          auto-resume after restart;
    §151/§152  deadline + cost/limit bounds;
    §153  no infinite loops (hard caps everywhere);
    §154/§155  progress reporting ("3/7 steps");
    §140  operational plan persisted — never chain-of-thought.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.paths import DATA_ROOT
from app.tools.grounding import GroundingLedger, initial_verification_status
from app.tools.redaction import redact_secrets


class TaskState(StrEnum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_JOB = "WAITING_FOR_JOB"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


_DESTRUCTIVE_RISKS = {"DESTRUCTIVE", "CRITICAL"}
_JOB_TOOLS = {"job_start", "browser_download_track", "wait_download"}

_DEFAULT_MAX_STEPS = 20
_DEFAULT_MAX_RETRIES_PER_STEP = 2


class TaskStep(BaseModel):
    step_id: str = Field(min_length=1, max_length=60)
    tool: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    resource: str = Field(default="", max_length=120)
    depends_on: list[str] = Field(default_factory=list)
    status: str = StepStatus.PENDING.value
    verification: dict[str, Any] = Field(default_factory=dict)
    auto_rollback: bool = False
    transaction_id: str | None = None
    approval_id: str | None = None
    job_id: str | None = None
    retries: int = 0
    last_error: str = ""
    result_summary: str = ""


class OperatorTask(BaseModel):
    task_id: str = Field(default_factory=lambda: f"task_{os.urandom(5).hex()}")
    goal: str = Field(min_length=2, max_length=1000)
    steps: list[TaskStep] = Field(min_length=0, max_length=40)
    state: str = TaskState.PLANNING.value
    resources: list[str] = Field(default_factory=list)
    verification_plan: str = Field(default="", max_length=500)
    created_at: float = Field(default_factory=time.time)
    deadline_at: float | None = None
    finished_at: float | None = None
    failure_reason: str = ""
    turn_id: str | None = None

    def progress(self) -> dict:
        total = len(self.steps)
        done = sum(1 for step in self.steps if step.status == StepStatus.SUCCEEDED.value)
        return {"done": done, "total": total, "label": f"{done}/{total} steps"}

    def public_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "state": self.state,
            "progress": self.progress(),
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
            "finished_at": self.finished_at,
            "failure_reason": redact_secrets(self.failure_reason)[:200],
            "verification_plan": self.verification_plan[:200],
            "steps": [
                {
                    "step_id": step.step_id,
                    "tool": step.tool,
                    "status": step.status,
                    "depends_on": step.depends_on,
                    "retries": step.retries,
                    "result_summary": step.result_summary[:160],
                }
                for step in self.steps
            ],
        }


class TaskValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

class OperatorTaskManager:
    def __init__(self, registry, approvals=None, jobs=None, recovery=None,
                 event_bus=None, *, database_path: Path | None = None,
                 max_steps: int = _DEFAULT_MAX_STEPS) -> None:
        self.registry = registry
        self.approvals = approvals
        self.jobs = jobs
        self.recovery = recovery
        self.event_bus = event_bus
        self.database_path = database_path or (DATA_ROOT / "kazumi.db")
        self.max_steps = max_steps
        self._tasks: dict[str, OperatorTask] = {}
        self._runners: dict[str, asyncio.Task] = {}
        self._approval_wakeups: dict[str, asyncio.Event] = {}
        self._approval_granted: dict[str, bool] = {}
        self._lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._last_emitted_states: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        await self._initialize_store()
        await self.resume_pending()

    def start(self) -> None:
        for runner in list(self._runners.values()):
            if runner.done():
                self._runners.pop(runner.get_name(), None)

    async def shutdown(self) -> None:
        for runner in self._runners.values():
            runner.cancel()
        for runner in self._runners.values():
            try:
                await runner
            except asyncio.CancelledError:
                pass
        self._runners.clear()

    # ---------------------------------------------------------------------- create
    async def create_task(self, goal: str, steps: list[dict[str, Any]], *,
                          verification_plan: str = "", deadline_seconds: int | None = None,
                          turn_id: str | None = None) -> dict:
        if not goal.strip() or len(goal) > 1000:
            raise TaskValidationError("INVALID_GOAL", "goal obrigatório (≤1000 chars).")
        if not steps or len(steps) > self.max_steps:
            raise TaskValidationError("STEP_LIMIT",
                                      f"Entre 1 e {self.max_steps} steps (§152 bounds).")
        known_tools = {
            descriptor["name"]
            for descriptor in self.registry.descriptions()
            if descriptor.get("enabled_for_llm", True) is True
        }
        parsed_steps: list[TaskStep] = []
        for raw in steps:
            tool = str(raw.get("tool") or "").strip()
            if not tool:
                raise TaskValidationError("INVALID_STEP", "Cada step precisa de 'tool'.")
            if tool not in known_tools:
                raise TaskValidationError(
                    "TOOL_NOT_EXPOSED",
                    f"Tool '{tool}' não está autorizada para composição por tarefa.",
                )
            step_id = str(raw.get("step_id") or f"step_{len(parsed_steps)+1}")
            parsed_steps.append(TaskStep(
                step_id=step_id,
                tool=tool,
                params=dict(raw.get("params") or {}),
                resource=str(raw.get("resource") or "")[:120],
                depends_on=list(raw.get("depends_on") or []),
                verification=dict(raw.get("verification") or {}),
                auto_rollback=bool(raw.get("auto_rollback")),
            ))
        task = OperatorTask(
            goal=goal.strip(),
            steps=parsed_steps,
            verification_plan=verification_plan[:500],
            deadline_at=(time.time() + max(30, min(int(deadline_seconds or 1800), 7200)))
            if deadline_seconds else None,
            turn_id=turn_id,
        )
        task.state = TaskState.PLANNING.value
        async with self._lock:
            self._tasks[task.task_id] = task
        await self._save(task)
        return {"success": True, "task": task.public_dict()}

    async def run_task(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if not task:
            return {"success": False, "error_code": "TASK_NOT_FOUND"}
        if task_id in self._runners and not self._runners[task_id].done():
            return {"success": False, "error_code": "TASK_ALREADY_RUNNING"}
        runner = asyncio.create_task(self._execute(task), name=task_id)
        self._runners[task_id] = runner
        return {"success": True, "task": task.public_dict()}

    # --------------------------------------------------------------------- resume
    async def resume_pending(self) -> dict:
        """§149/§150: after restart, only SAFE pending work auto-resumes."""
        resumed, blocked_destructive = [], 0
        for task in list(self._tasks.values()) or await self._load_all():
            if task.state not in {TaskState.RUNNING.value, TaskState.WAITING_FOR_USER.value,
                                  TaskState.WAITING_FOR_JOB.value, TaskState.VERIFYING.value}:
                continue
            has_pending_destructive = False
            for step in task.steps:
                if step.status != StepStatus.PENDING.value:
                    continue
                risk = _tool_risk(self.registry, step.tool)
                if risk in _DESTRUCTIVE_RISKS:
                    step.status = StepStatus.BLOCKED.value  # §150
                    has_pending_destructive = True
                    blocked_destructive += 1
            if all(step.status == StepStatus.BLOCKED.value for step in task.steps if step.status):
                task.state = TaskState.WAITING_FOR_USER.value
                await self._save(task)
                continue
            self._tasks.setdefault(task.task_id, task)
            outcome = await self.run_task(task.task_id)
            if outcome.get("success"):
                resumed.append(task.task_id)
        return {"success": True, "resumed": resumed, "blocked_destructive_steps": blocked_destructive}

    # -------------------------------------------------------------------- control
    async def status(self, task_id: str) -> dict:
        task = self._tasks.get(task_id) or await self._load(task_id)
        if not task:
            return {"success": False, "error_code": "TASK_NOT_FOUND"}
        return {"success": True, "task": task.public_dict()}

    async def list_tasks(self, include_terminal: bool = True) -> dict:
        items = [task.public_dict() for task in self._tasks.values()]
        if not include_terminal:
            items = [item for item in items
                     if item["state"] not in {TaskState.SUCCEEDED.value, TaskState.FAILED.value,
                                              TaskState.CANCELLED.value}]
        return {"success": True, "tasks": items, "count": len(items)}

    async def cancel(self, task_id: str, reason: str = "operator_cancelled") -> dict:
        task = self._tasks.get(task_id)
        if not task:
            return {"success": False, "error_code": "TASK_NOT_FOUND"}
        if task.state in {TaskState.SUCCEEDED.value, TaskState.FAILED.value,
                          TaskState.CANCELLED.value}:
            return {"success": False, "error_code": "TASK_ALREADY_FINISHED", "state": task.state}
        runner = self._runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        task.state = TaskState.CANCELLED.value
        task.finished_at = time.time()
        task.failure_reason = reason[:200]
        await self._save(task)
        await self._emit("TASK_FINISHED", task_id=task_id, state=task.state)
        return {"success": True, "task": task.public_dict()}

    # ------------------------------------------------------------------- executor
    async def _execute(self, task: OperatorTask) -> None:
        ledger = GroundingLedger()
        task.state = TaskState.RUNNING.value
        await self._save(task)
        await self._emit("TASK_STARTED", task_id=task.task_id)
        completed_ids = {step.step_id for step in task.steps
                         if step.status == StepStatus.SUCCEEDED.value}
        try:
            while True:
                step = self._next_ready_step(task, completed_ids)
                if step is None:
                    statuses = {s.status for s in task.steps}
                    if StepStatus.PENDING.value in statuses:
                        # Há steps pendentes mas nenhum pronto: dependência
                        # falhou/bloqueada — deadlock operacional.
                        task.state = TaskState.WAITING_FOR_USER.value
                    elif StepStatus.BLOCKED.value in statuses:
                        task.state = TaskState.WAITING_FOR_USER.value  # blocked por approval/destrutivo
                    else:
                        task.state = TaskState.SUCCEEDED.value
                    break
                if task.deadline_at and time.time() >= task.deadline_at:
                    task.state = TaskState.FAILED.value
                    task.failure_reason = "DEADLINE_EXCEEDED"
                    break
                outcome = await self._run_step(task, step, ledger)
                if outcome == "WAIT_USER":
                    task.state = TaskState.WAITING_FOR_USER.value
                    await self._save(task)
                    granted = await self._wait_for_approval(task.task_id, step.step_id)
                    if not granted:
                        step.status = StepStatus.FAILED.value
                        step.last_error = "APPROVAL_DENIED"
                        task.state = TaskState.FAILED.value
                        task.failure_reason = "approval negado pelo operador"
                        break
                    if step.approval_id:
                        step.params["approval_id"] = step.approval_id  # consome vínculo único
                    step.status = StepStatus.PENDING.value  # re-executa com approval vinculado
                    task.state = TaskState.RUNNING.value
                    continue
                if outcome == "WAIT_JOB":
                    task.state = TaskState.WAITING_FOR_JOB.value
                    job_ok = await self._wait_for_job(step, task)
                    task.state = TaskState.VERIFYING.value
                    if job_ok:
                        step.status = StepStatus.SUCCEEDED.value
                        completed_ids.add(step.step_id)
                    else:
                        step.status = StepStatus.FAILED.value
                        task.state = TaskState.FAILED.value
                        task.failure_reason = f"job do step '{step.step_id}' falhou"
                        break
                    continue
                if outcome == "RETRY":
                    continue
                if outcome.startswith("FAIL"):
                    if step.auto_rollback and step.transaction_id and self.recovery is not None:
                        task.state = TaskState.RECOVERING.value
                        rollback = await self.recovery.rollback(step.transaction_id, auto=True)
                        step.result_summary = f"rollback={rollback.get('state')}"
                        task.state = TaskState.FAILED.value
                        task.failure_reason = f"step '{step.step_id}': {step.last_error}"
                    break
                completed_ids.add(step.step_id)
                if outcome == "VERIFY":
                    task.state = TaskState.VERIFYING.value
                    verified = await self._verify_step(step)
                    task.state = TaskState.RUNNING.value
                    if step.verification and step.verification.get("required") and not verified:
                        step.status = StepStatus.FAILED.value
                        step.last_error = "VERIFICATION_FAILED"
                        continue  # retry path handles the cap
        except asyncio.CancelledError:
            task.state = TaskState.CANCELLED.value
            raise
        finally:
            task.finished_at = time.time()
            if task.state == TaskState.RUNNING.value:
                failed_steps = any(s.status == StepStatus.FAILED.value for s in task.steps)
                task.state = TaskState.FAILED.value if failed_steps else TaskState.SUCCEEDED.value
            if not task.failure_reason and task.state == TaskState.SUCCEEDED.value:
                task.failure_reason = ""
            await self._save(task)
            await self._emit("TASK_FINISHED", task_id=task.task_id, state=task.state)

    def _next_ready_step(self, task: OperatorTask, done: set[str]) -> TaskStep | None:
        by_id = {step.step_id: step for step in task.steps}
        for step in task.steps:
            if step.status != StepStatus.PENDING.value:
                continue
            ready = True
            for dependency in step.depends_on:
                target = by_id.get(dependency)
                satisfied = dependency in done or (
                    target is not None and target.status == StepStatus.SKIPPED.value
                )
                if not satisfied:
                    ready = False
                    break
            if ready:
                return step
        return None

    async def _run_step(self, task: OperatorTask, step: TaskStep,
                        ledger: GroundingLedger) -> str:
        """Returns: OK | VERIFY | RETRY | WAIT_USER | WAIT_JOB | FAIL:<code>"""
        step.status = StepStatus.RUNNING.value
        risk = _tool_risk(self.registry, step.tool)
        params = dict(step.params)
        if self.jobs is not None and step.tool == "job_start":
            pass  # jobs manager handles its own identity/locks
        if risk in _DESTRUCTIVE_RISKS and step.transaction_id is None and self.recovery is not None:
            target = str(params.get("path") or "")
            if target:
                try:
                    prepared = await self.recovery.prepare_file_backup(target, action=f"{task.task_id}:{step.tool}")
                    step.transaction_id = prepared.get("transaction_id")
                except Exception:  # noqa: BLE001 - recovery é best-effort (§159)
                    pass
        started = time.perf_counter()
        try:
            result = await self.registry.execute(step.tool, params, exposure="llm")
            ok = bool(result.ok)
            data = result.data
            risk_value = getattr(result.risk, "value", str(result.risk))
        except Exception as exc:  # noqa: BLE001
            ok, data, risk_value = False, {"message": redact_secrets(str(exc)[:200])}, risk
        duration_ms = round((time.perf_counter() - started) * 1000, 1)

        if ok:
            import hashlib
            import json as _json

            observation = ledger.record(
                tool_call_id=f"tsk_{os.urandom(4).hex()}",
                tool_name=step.tool,
                result_data=data,
                risk_level=risk_value,
                resource_key=step.resource or f"task:{task.task_id}",
                arguments_fingerprint=hashlib.sha256(
                    _json.dumps(params, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                turn_id=None,
            )
            del observation
            verification_status = initial_verification_status(True, risk_value)
            if step.transaction_id and self.recovery is not None:
                post_hash = _file_hash(str(params.get("path") or ""))
                if post_hash:
                    self.recovery.mark_written(step.transaction_id, post_hash)
                    await self.recovery.commit(step.transaction_id)
                    step.transaction_id = None
            step.status = StepStatus.SUCCEEDED.value
            step.result_summary = _summary(data, duration_ms)
            await self._save(task)
            return "VERIFY" if verification_status == "EXECUTED" else "OK"

        error_code = str(data.get("error_code") or "")
        if error_code == "APPROVAL_REQUIRED":
            step.approval_id = data.get("approval_id")
            return "WAIT_USER"
        if step.tool in _JOB_TOOLS:
            job_id = str(data.get("job_id") or (data.get("job") or {}).get("job_id") or "")
            if error_code == "APPROVAL_REQUIRED":
                return "WAIT_USER"
            step.job_id = job_id
            return "WAIT_JOB"
        step.retries += 1
        step.last_error = f"{error_code}: {str(data.get('message') or '')[:140]}"
        if step.retries > _DEFAULT_MAX_RETRIES_PER_STEP:
            step.status = StepStatus.FAILED.value
            await self._save(task)
            return f"FAIL:{error_code}"
        step.status = StepStatus.PENDING.value  # transient failure → retry (§293)
        await self._save(task)
        await asyncio.sleep(1.0)
        return "RETRY"

    async def _verify_step(self, step: TaskStep) -> bool:
        probe_tool = str((step.verification or {}).get("tool") or "")
        if not probe_tool or self.registry is None:
            return False
        probe_params = dict((step.verification or {}).get("params") or {})
        try:
            result = await self.registry.execute(probe_tool, probe_params, exposure="llm")
            output = json.dumps(result.data, ensure_ascii=False, default=str)
            expect = str((step.verification or {}).get("expect_contains") or "")
            return bool(result.ok) and (not expect or expect.casefold() in output.casefold())
        except Exception:  # noqa: BLE001
            return False

    async def _wait_for_job(self, step: TaskStep, task: OperatorTask) -> bool:
        """§182: event-ish wait — light polling only while THIS task waits."""
        job_id = getattr(step, "job_id", "")
        deadline = time.time() + 3600
        while time.time() < deadline:
            if task.deadline_at and time.time() >= task.deadline_at:
                return False
            if self.jobs is None or not job_id:
                await asyncio.sleep(2.0)
                return True
            status = await self.jobs.status(job_id)
            state = str(((status or {}).get("job") or {}).get("state") or "")
            if state in {"SUCCEEDED"}:
                return True
            if state in {"FAILED", "CANCELLED", "UNKNOWN"}:
                return False
            await asyncio.sleep(2.0)
        return False

    def _register_approval_wakeup(self, task_id: str, step_id: str) -> None:
        key = f"{task_id}:{step_id}"
        if key not in self._approval_wakeups:
            self._approval_wakeups[key] = asyncio.Event()

    async def notify_approval(self, task_id: str, step_id: str, granted: bool) -> None:
        key = f"{task_id}:{step_id}"
        self._approval_granted[key] = granted
        event = self._approval_wakeups.get(key)
        if event is not None:
            event.set()

    async def _wait_for_approval(self, task_id: str, step_id: str,
                                 timeout_seconds: float = 900.0) -> bool:
        key = f"{task_id}:{step_id}"
        self._register_approval_wakeup(task_id, step_id)
        event = self._approval_wakeups[key]
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return False
        return self._approval_granted.get(key, False)

    # ----------------------------------------------------------------------- store
    async def _initialize_store(self) -> None:
        def work() -> None:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operator_tasks (
                        task_id TEXT PRIMARY KEY,
                        goal TEXT, state TEXT, created_at REAL,
                        deadline_at REAL, finished_at REAL,
                        verification_plan TEXT, failure_reason TEXT,
                        steps_json TEXT
                    )
                    """
                )
        await asyncio.to_thread(work)

    async def _save(self, task: OperatorTask) -> None:
        def work() -> None:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO operator_tasks (task_id, goal, state, created_at, deadline_at,
                                                finished_at, verification_plan, failure_reason,
                                                steps_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        state=excluded.state, finished_at=excluded.finished_at,
                        failure_reason=excluded.failure_reason, steps_json=excluded.steps_json
                    """,
                    (
                        task.task_id, task.goal, task.state, task.created_at,
                        task.deadline_at, task.finished_at, task.verification_plan,
                        task.failure_reason, _steps_payload(task),
                    ),
                )
        await asyncio.to_thread(work)
        if self._last_emitted_states.get(task.task_id) != task.state:
            self._last_emitted_states[task.task_id] = task.state
            await self._emit(
                "TASK_STATE_CHANGED",
                task_id=task.task_id,
                goal=task.goal,
                state=task.state,
                source="operator_task",
            )

    async def _load_all(self) -> list[OperatorTask]:
        def work() -> list[OperatorTask]:
            with sqlite3.connect(self.database_path) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM operator_tasks ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
                tasks = []
                for row in rows:
                    try:
                        data = dict(row)
                        data["steps"] = json.loads(data.get("steps_json") or "[]")
                        data.pop("steps_json", None)
                        tasks.append(OperatorTask.model_validate(data))
                    except Exception:  # noqa: BLE001 - corrupted rows are skipped
                        continue
                return tasks

        return await asyncio.to_thread(work)

    async def _load(self, task_id: str) -> OperatorTask | None:
        if task_id in self._tasks:
            return self._tasks[task_id]
        for task in await self._load_all():
            if task.task_id == task_id:
                self._tasks[task_id] = task
                return task
        return None

    async def _emit(self, event_name: str, **payload: Any) -> None:
        if self.event_bus is None:
            return
        from app.events import EventType

        try:
            event = EventType(event_name)
        except ValueError:
            event = EventType.ERROR
        try:
            payload.setdefault("source", "operator_task")
            await self.event_bus.publish(event, **payload)
        except Exception:  # noqa: BLE001
            pass


def _tool_risk(registry, tool: str) -> str:
    try:
        preflight = registry.preflight(tool, {})
        return str(preflight.get("risk_level") or "READ_ONLY")
    except Exception:  # noqa: BLE001 - unknown tools treated fail-closed
        return "ELEVATED"


def _file_hash(path: str) -> str | None:
    import hashlib
    from pathlib import Path

    target = Path(path)
    if not path or not target.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _steps_payload(task: OperatorTask) -> str:
    payload = [step.model_dump(mode="json") for step in task.steps]
    text = json.dumps(payload, ensure_ascii=False)
    return redact_secrets(text)[:60000]


def _summary(data: dict, duration_ms: float) -> str:
    message = data.get("message") or data.get("error_code") or "ok"
    verified = data.get("effect_verified")
    suffix = f" | effect_verified={verified}" if verified is not None else ""
    return f"{str(message)[:140]}{suffix} | {duration_ms}ms"
