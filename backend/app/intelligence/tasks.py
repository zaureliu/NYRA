from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

import aiosqlite

from app.intelligence.budget import ActionBudget, BudgetLimits
from app.intelligence.capabilities import CapabilityRegistryV2
from app.intelligence.models import AutonomousTaskSpec, AutonomousTaskState, TraceStage
from app.intelligence.storage import IntelligenceStore
from app.intelligence.tracing import TraceService


TaskHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class AutonomousTaskEngine:
    def __init__(self, store: IntelligenceStore, capabilities: CapabilityRegistryV2,
                 traces: TraceService, *, event_bus=None, max_concurrent: int = 1) -> None:
        self.store, self.capabilities, self.traces = store, capabilities, traces
        self.event_bus = event_bus
        self.handlers: dict[str, tuple[TaskHandler, str]] = {}
        self._runner: asyncio.Task | None = None
        self._active: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self.last_error: str | None = None

    def register(self, action: str, handler: TaskHandler, *, risk: str = "READ_ONLY") -> None:
        self.handlers[action] = (handler, risk)

    async def start(self) -> None:
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute("UPDATE autonomous_tasks_v2 SET state='QUEUED' WHERE state='RUNNING'")
            await db.commit()
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._loop(), name="nyra-autonomous-tasks-v2")
        if self.event_bus is not None:
            await self.event_bus.subscribe(self._on_event)

    async def stop(self) -> None:
        if self.event_bus is not None:
            await self.event_bus.unsubscribe(self._on_event)
        if self._runner and not self._runner.done():
            self._runner.cancel()
        for task in tuple(self._active.values()):
            task.cancel()
        pending = [task for task in (self._runner, *self._active.values()) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._runner = None
        self._active.clear()

    async def create(self, spec: AutonomousTaskSpec, *, approved: bool = False) -> AutonomousTaskSpec:
        if spec.action not in self.handlers:
            raise ValueError("TASK_ACTION_NOT_REGISTERED")
        if spec.trigger == "event" and not spec.conditions.get("event_type"):
            raise ValueError("TASK_EVENT_TYPE_REQUIRED")
        if spec.trigger == "conditional" and not any(
            key in spec.conditions for key in ("capability_available", "capability_states", "not_before")
        ):
            raise ValueError("TASK_CONDITION_UNSUPPORTED")
        now = datetime.now(timezone.utc)
        # Identity and runtime state are server-owned. A local API caller may
        # describe a task, but cannot overwrite an existing row or forge a
        # completed/approved execution through the request body.
        spec = spec.model_copy(deep=True, update={
            "task_id": f"task_{uuid4().hex}",
            "state": AutonomousTaskState.CREATED,
            "retries": 0,
            "last_run": None,
            "result": {},
            "created_at": now,
            "updated_at": now,
        })
        declared_risk = self.handlers[spec.action][1]
        if (spec.approval_mode == "always"
                or declared_risk in {"ELEVATED", "DESTRUCTIVE", "CRITICAL"}
                or spec.risk in {"ELEVATED", "DESTRUCTIVE", "CRITICAL"}):
            if not approved:
                spec.state = AutonomousTaskState.WAITING_APPROVAL
        for capability in spec.required_capabilities:
            if not await self.capabilities.available(capability):
                spec.state = AutonomousTaskState.WAITING
                spec.result = {"error_code": "CAPABILITY_UNAVAILABLE", "capability": capability}
                break
        if spec.state == AutonomousTaskState.CREATED:
            spec.state = (
                AutonomousTaskState.WAITING
                if spec.trigger in {"event", "conditional"}
                else AutonomousTaskState.QUEUED
            )
        if spec.trigger in {"schedule", "recurring"} and spec.next_run is None:
            spec.next_run = self._next_run(spec, datetime.now(timezone.utc))
        await self._save(spec)
        return spec

    async def list(self, *, include_terminal: bool = True) -> list[AutonomousTaskSpec]:
        where = "" if include_terminal else "WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED')"
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute(f"SELECT document FROM autonomous_tasks_v2 {where} ORDER BY updated_at DESC")).fetchall()
        return [AutonomousTaskSpec.model_validate_json(row[0]) for row in rows]

    async def get(self, task_id: str) -> AutonomousTaskSpec | None:
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT document FROM autonomous_tasks_v2 WHERE task_id=?", (task_id,))).fetchone()
        return AutonomousTaskSpec.model_validate_json(row[0]) if row else None

    async def cancel(self, task_id: str) -> bool:
        task = await self.get(task_id)
        if not task:
            return False
        active = self._active.get(task_id)
        if active:
            active.cancel()
        task.state = AutonomousTaskState.CANCELLED
        task.updated_at = datetime.now(timezone.utc)
        await self._save(task)
        return True

    async def pause(self, task_id: str) -> bool:
        task = await self.get(task_id)
        if not task:
            return False
        task.state = AutonomousTaskState.PAUSED
        task.updated_at = datetime.now(timezone.utc)
        await self._save(task)
        return True

    async def resume(self, task_id: str) -> bool:
        task = await self.get(task_id)
        if not task or task.state not in {AutonomousTaskState.PAUSED, AutonomousTaskState.WAITING}:
            return False
        task.state = (
            AutonomousTaskState.WAITING
            if task.trigger in {"event", "conditional"}
            else AutonomousTaskState.QUEUED
        )
        task.updated_at = datetime.now(timezone.utc)
        await self._save(task)
        return True

    async def run_now(self, task_id: str) -> AutonomousTaskSpec:
        task = await self.get(task_id)
        if not task:
            raise KeyError("TASK_NOT_FOUND")
        if task.state == AutonomousTaskState.WAITING_APPROVAL:
            raise PermissionError("TASK_APPROVAL_REQUIRED")
        if task.state in {AutonomousTaskState.CANCELLED, AutonomousTaskState.PAUSED}:
            raise RuntimeError("TASK_NOT_RUNNABLE")
        await self._execute(task)
        return await self.get(task_id) or task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = datetime.now(timezone.utc)
            try:
                pending = await self.list(include_terminal=False)
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - persistence outage degrades without loop death
                self.last_error = type(error).__name__
                await asyncio.sleep(4)
                continue
            for task in pending:
                if task.task_id in self._active:
                    continue
                if task.state == AutonomousTaskState.WAITING:
                    if task.trigger == "event":
                        continue
                    if not await self._conditions_ready(task):
                        continue
                    task.state = AutonomousTaskState.QUEUED
                    task.updated_at = datetime.now(timezone.utc)
                    await self._save(task)
                if task.state != AutonomousTaskState.QUEUED:
                    continue
                if task.next_run and task.next_run > now:
                    continue
                running = asyncio.create_task(self._execute(task), name=f"nyra-task-v2-{task.task_id[-8:]}")
                self._active[task.task_id] = running
                running.add_done_callback(lambda _, key=task.task_id: self._active.pop(key, None))

    async def _conditions_ready(self, task: AutonomousTaskSpec) -> bool:
        if not all(await asyncio.gather(*(
            self.capabilities.available(capability) for capability in task.required_capabilities
        ))):
            return False
        conditions = task.conditions
        requested = conditions.get("capability_available")
        requested_ids = [requested] if isinstance(requested, str) else requested if isinstance(requested, list) else []
        if requested_ids and not all(await asyncio.gather(*(
            self.capabilities.available(str(capability)) for capability in requested_ids
        ))):
            return False
        expected_states = conditions.get("capability_states")
        if isinstance(expected_states, dict) and expected_states:
            snapshot = await self.capabilities.snapshot()
            states = {item["id"]: item["state"] for item in snapshot.get("capabilities", [])}
            if any(states.get(str(identity)) != str(state) for identity, state in expected_states.items()):
                return False
        not_before = conditions.get("not_before")
        if not_before:
            try:
                if datetime.fromisoformat(str(not_before)).astimezone(timezone.utc) > datetime.now(timezone.utc):
                    return False
            except ValueError as error:
                raise ValueError("TASK_CONDITION_TIME_INVALID") from error
        return bool(requested_ids or expected_states or not_before)

    async def _on_event(self, event) -> None:
        event_type = str(getattr(getattr(event, "type", None), "value", getattr(event, "type", "")))
        payload = getattr(event, "payload", {}) or {}
        for task in await self.list(include_terminal=False):
            if task.trigger != "event" or task.state != AutonomousTaskState.WAITING:
                continue
            expected = task.conditions.get("event_type")
            expected_types = [expected] if isinstance(expected, str) else expected if isinstance(expected, list) else []
            if event_type not in {str(value) for value in expected_types}:
                continue
            expected_source = task.conditions.get("source")
            if expected_source and str(payload.get("source") or "") != str(expected_source):
                continue
            expected_payload = task.conditions.get("payload_equals")
            if isinstance(expected_payload, dict) and any(payload.get(key) != value for key, value in expected_payload.items()):
                continue
            task.state = AutonomousTaskState.QUEUED
            task.updated_at = datetime.now(timezone.utc)
            task.result = {
                "trigger": "event",
                "event_type": event_type,
                "event_id": str(getattr(event, "id", "")),
            }
            await self._save(task)

    async def _execute(self, task: AutonomousTaskSpec) -> None:
        async with self._semaphore:
            trace_id = self.traces.new()
            budget = ActionBudget(BudgetLimits(max_tool_calls=1, max_retries=task.max_retries,
                                               timeout_seconds=task.timeout_seconds))
            task.state = AutonomousTaskState.RUNNING
            task.last_run = task.updated_at = datetime.now(timezone.utc)
            await self._save(task)
            await self.traces.record(trace_id, TraceStage.PLAN, component="task_engine", operation=task.action,
                                     payload={"objective": task.objective, "risk": task.risk}, task_id=task.task_id)
            try:
                budget.consume("tool")
                handler, _risk = self.handlers[task.action]
                result = await asyncio.wait_for(handler(task.parameters), timeout=task.timeout_seconds)
                verified = bool(result.get("effect_verified", result.get("success", False)))
                await self.traces.record(trace_id, TraceStage.VERIFICATION, component="task_engine",
                                         operation=task.action, payload={"effect_verified": verified}, task_id=task.task_id)
                if not verified:
                    raise RuntimeError("TASK_EFFECT_NOT_VERIFIED")
                task.result = result
                if task.trigger == "recurring":
                    task.state = AutonomousTaskState.QUEUED
                    task.next_run = self._next_run(task, datetime.now(timezone.utc))
                else:
                    task.state = AutonomousTaskState.COMPLETED
            except asyncio.CancelledError:
                task.state = AutonomousTaskState.CANCELLED
                task.result = {"error_code": "TASK_CANCELLED"}
                raise
            except Exception as error:  # includes timeout/budget; task failure is isolated
                task.retries += 1
                task.result = {"error_code": getattr(error, "code", type(error).__name__)}
                if task.retries <= task.max_retries:
                    task.state = AutonomousTaskState.QUEUED
                    task.next_run = datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** task.retries))
                else:
                    task.state = AutonomousTaskState.FAILED
            finally:
                task.updated_at = datetime.now(timezone.utc)
                await self._save(task)

    @staticmethod
    def _next_run(task: AutonomousTaskSpec, now: datetime) -> datetime | None:
        if task.trigger not in {"schedule", "recurring"}:
            return None
        schedule = str(task.schedule or "").strip().casefold()
        if schedule.startswith("every:"):
            try:
                seconds = max(5, min(86400 * 30, int(schedule.split(":", 1)[1])))
                return now + timedelta(seconds=seconds)
            except ValueError as error:
                raise ValueError("TASK_SCHEDULE_INVALID") from error
        try:
            return datetime.fromisoformat(schedule).astimezone(timezone.utc)
        except ValueError as error:
            raise ValueError("TASK_SCHEDULE_INVALID") from error

    async def _save(self, task: AutonomousTaskSpec) -> None:
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                """INSERT INTO autonomous_tasks_v2(task_id,document,state,next_run,updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET document=excluded.document,state=excluded.state,
                   next_run=excluded.next_run,updated_at=excluded.updated_at""",
                (task.task_id, task.model_dump_json(), task.state.value,
                 task.next_run.isoformat() if task.next_run else None, task.updated_at.isoformat()),
            )
            await db.commit()
