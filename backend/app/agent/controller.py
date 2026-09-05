from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from typing import Any
from uuid import uuid4

from app.agent.context import current_agent_run_id
from app.agent.models import AgentLoopRuntime, AgentRun, AgentRunState, AgentRunStatus, AgentStep
from app.agent.store import AgentRunStore
from app.core.config import Settings
from app.events import EventBus, EventType
from app.llm import LLMMessage, LLMProvider
from app.tools.agent import ToolAgentLoop
from app.tools.grounding import ToolObservation, initial_verification_status
from app.tools.redaction import redact_secrets
from app.intelligence.budget import ActionBudget, BudgetExceeded, BudgetLimits


logger = logging.getLogger("kazumi.agent")


def _persistent_fingerprint_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Remove conteúdo privado antes de produzir fingerprints persistentes.

    O loop pode usar os argumentos completos em memória para dedupe durante o
    turno, mas o Agent Run salvo em disco deve reter somente metadados seguros.
    """
    safe = {key: value for key, value in arguments.items() if key != "approval_id"}
    if tool == "clipboard_write_text" and "text" in safe:
        safe["text"] = {
            "redacted": True,
            "length": len(str(safe["text"])),
        }
    return safe


class AgentController:
    def __init__(self, settings: Settings, event_bus: EventBus, llm: LLMProvider, registry) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.llm = llm
        self.registry = registry
        self.store = AgentRunStore(settings.database_path)
        self._runs: dict[str, AgentRun] = {}
        self._cancellations: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._resource_locks: dict[str, asyncio.Lock] = {}
        self._resource_owners: dict[str, str] = {}
        self._runs_by_turn: dict[str, str] = {}
        self._budgets: dict[str, ActionBudget] = {}
        self._last_budget: dict[str, Any] | None = None

    async def initialize(self) -> None:
        await self.store.initialize()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.agent_enabled,
            "read_only": self.settings.agent_read_only,
            "auto_remediation": self.settings.agent_auto_remediation,
            "max_steps": self.settings.agent_max_steps,
            "max_tool_calls": self.settings.agent_max_tool_calls,
            "max_runtime_seconds": self.settings.agent_max_runtime_seconds,
            "active_runs": [run_id for run_id, task in self._tasks.items() if not task.done()],
            "action_budget": {
                "active": len(self._budgets),
                "last": self._last_budget,
                "max_tool_calls": self.settings.agent_max_tool_calls,
                "max_planner_iterations": self.settings.agent_max_steps,
                "max_consecutive_failures": self.settings.agent_max_consecutive_failures,
            },
        }

    async def run(
        self,
        messages: list[LLMMessage],
        goal: str,
        *,
        resume_run_id: str | None = None,
        external_cancel_event: asyncio.Event | None = None,
        turn_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        if not self.settings.agent_enabled:
            return await ToolAgentLoop(
                self.llm, self.registry, min(1, self.settings.shell_max_calls_per_turn),
            ).run(messages, turn_id=turn_id)
        run = await self._resume_or_create(
            goal,
            resume_run_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
        )
        if turn_id:
            run.turn_id = turn_id
        if conversation_id and not run.conversation_id:
            run.conversation_id = conversation_id
        cancellation = self._cancellations.setdefault(run.id, asyncio.Event())
        action_budget = ActionBudget(BudgetLimits(
            max_tool_calls=self.settings.agent_max_tool_calls,
            max_retries=max(0, self.settings.agent_max_identical_repeats - 1),
            max_planner_iterations=self.settings.agent_max_steps,
            max_consecutive_failures=self.settings.agent_max_consecutive_failures,
            timeout_seconds=self.settings.agent_max_runtime_seconds,
            max_restarts=0,
            destructive_actions=0,
            network_actions=self.settings.agent_max_tool_calls,
        ))
        self._budgets[run.id] = action_budget
        current_task = asyncio.current_task()
        if current_task:
            self._tasks[run.id] = current_task
        if external_cancel_event and external_cancel_event.is_set():
            cancellation.set()

        async def transition(state: AgentRunState) -> None:
            run.state = state
            run.updated_at = datetime.now(timezone.utc)
            if state == AgentRunState.WAITING_APPROVAL:
                run.status = AgentRunStatus.WAITING_APPROVAL
            elif state == AgentRunState.COMPLETE:
                run.status = (
                    AgentRunStatus.COMPLETED_WITH_UNVERIFIED_ACTION
                    if runtime.unverified_action
                    else AgentRunStatus.COMPLETED
                )
            elif state == AgentRunState.FAILED:
                run.status = AgentRunStatus.FAILED
            elif state == AgentRunState.CANCELLED:
                run.status = AgentRunStatus.CANCELLED
            else:
                run.status = AgentRunStatus.RUNNING
            await self.store.save(run)
            await self.event_bus.publish(
                EventType.AGENT_RUN_STATE_CHANGED,
                agent_run_id=run.id, goal=run.goal, state=state.value, status=run.status.value,
                turn_id=run.turn_id, conversation_id=run.conversation_id,
            )

        async def record_step(tool: str, arguments: dict, preflight: dict, result: dict, observation: ToolObservation | None = None) -> None:
            try:
                action_budget.consume("tool")
                result_data_for_budget = result.get("data", result)
                if bool(result_data_for_budget.get("success", result.get("ok", False))):
                    action_budget.success()
                else:
                    action_budget.consume("failure")
            except BudgetExceeded as error:
                # AgentLoopRuntime remains the execution authority and permits
                # a bounded read-only verification after the primary-call cap.
                # Record the central budget signal without aborting that safety
                # verification or replacing its established stop reason.
                setattr(action_budget, "last_exceeded", error.code)
            run.tool_calls += 1
            target = str(preflight.get("host") or ("local" if tool == "system_shell" else tool))
            if tool == "remote_shell" and target not in run.host_targets:
                run.host_targets.append(target)
            command_value = str(arguments.get("command") or tool)
            result_data = result.get("data", result)
            summary_source = str(result_data.get("message") or result_data.get("stderr") or result_data.get("stdout") or "")
            safe_summary = redact_secrets(" ".join(summary_source.split()))[:400]
            command_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "tool": tool,
                        "arguments": _persistent_fingerprint_arguments(tool, arguments),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            result_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "success": result_data.get("success"), "exit_code": result_data.get("exit_code"),
                        "error_code": result_data.get("error_code"), "stdout": str(result_data.get("stdout", ""))[:2000],
                        "stderr": str(result_data.get("stderr", ""))[:2000],
                    }, sort_keys=True, ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            risk_value = str(preflight.get("risk_level") or result.get("risk") or "READ_ONLY")
            verification_status = (
                observation.verification_status
                if observation is not None
                else initial_verification_status(
                    result_data.get("success") if "success" in result_data else result.get("ok"),
                    risk_value,
                )
            )
            step = AgentStep(
                index=len(run.steps) + 1, state=run.state, tool=tool, target=target,
                operation=redact_secrets(f"{tool}: {command_value}")[:500],
                command_fingerprint=command_fingerprint, result_fingerprint=result_fingerprint,
                success=bool(result_data.get("success", result.get("ok", False))),
                risk_level=risk_value,
                verification_status=verification_status,
                tool_call_id=observation.tool_call_id if observation else None,
                execution_id=observation.execution_id if observation else None,
                summary=safe_summary,
            )
            run.steps.append(step)
            run.updated_at = datetime.now(timezone.utc)
            await self.store.save(run)
            await self.event_bus.publish(
                EventType.AGENT_RUN_STEP,
                agent_run_id=run.id, goal=run.goal, index=step.index, state=step.state.value,
                tool=tool, target=target, success=step.success, risk_level=step.risk_level,
                summary=step.summary, turn_id=run.turn_id, conversation_id=run.conversation_id,
            )

        async def acquire_resource(resource: str) -> bool:
            lock = self._resource_locks.setdefault(resource, asyncio.Lock())
            owner = self._resource_owners.get(resource)
            if owner and owner != run.id:
                try:
                    await asyncio.wait_for(lock.acquire(), 5)
                except TimeoutError:
                    return False
            elif not lock.locked():
                await lock.acquire()
            self._resource_owners[resource] = run.id
            return True

        async def release_resource(resource: str) -> None:
            lock = self._resource_locks.get(resource)
            if self._resource_owners.get(resource) == run.id:
                self._resource_owners.pop(resource, None)
                if lock and lock.locked():
                    lock.release()

        runtime = AgentLoopRuntime(
            run,
            max_steps=self.settings.agent_max_steps,
            max_tool_calls=self.settings.agent_max_tool_calls,
            max_runtime_seconds=self.settings.agent_max_runtime_seconds,
            max_identical_repeats=self.settings.agent_max_identical_repeats,
            max_consecutive_failures=self.settings.agent_max_consecutive_failures,
            read_only=self.settings.agent_read_only,
            cancellation=cancellation,
            transition=transition,
            record_step=record_step,
            acquire_resource=acquire_resource,
            release_resource=release_resource,
        )
        if any(term in goal.casefold() for term in ("verifica", "verificar", "saud", "diagnost", "por que", "problema", "estranh", "ambiente")):
            remote_target = self.registry.resolve_remote_target(goal)
            if remote_target:
                runtime.required_remote_host = remote_target["host"]
                runtime.required_remote_address = remote_target["address"]
        if re.search(r"(?i)(?:backend[^\n]{0,40}kazumi|kazumi[^\n]{0,40}backend)", goal):
            runtime.required_local_backend = True
            runtime.local_backend_port = self.settings.backend_port
            runtime.local_backend_root = str(self.settings.shell_default_working_directory)
        await self.event_bus.publish(
            EventType.AGENT_RUN_STARTED,
            agent_run_id=run.id, goal=run.goal, resumed=bool(resume_run_id), state=run.state.value,
            turn_id=run.turn_id, conversation_id=run.conversation_id,
        )
        token = current_agent_run_id.set(run.id)
        try:
            response = await asyncio.wait_for(
                ToolAgentLoop(
                    self.llm, self.registry, self.settings.shell_max_calls_per_turn,
                ).run(messages, runtime=runtime, turn_id=turn_id),
                timeout=self.settings.agent_max_runtime_seconds,
            )
            run.pending_approval_id = runtime.pending_approval_id
            run.final_summary = redact_secrets(response)[:2000]
            if runtime.pending_approval_id:
                await transition(AgentRunState.WAITING_APPROVAL)
            elif runtime.stop_reason:
                run.error = runtime.stop_reason
                await transition(AgentRunState.FAILED)
            else:
                await transition(AgentRunState.COMPLETE)
            await self.event_bus.publish(
                EventType.AGENT_RUN_FINISHED,
                agent_run_id=run.id, goal=run.goal, state=run.state.value,
                status=run.status.value, tool_calls=run.tool_calls, steps=len(run.steps),
                unverified_action=runtime.unverified_action,
                turn_id=run.turn_id, conversation_id=run.conversation_id,
            )
            logger.info(
                "agent_run_finished",
                extra={
                    "agent_run_id": run.id, "state": run.state.value, "tool_calls": run.tool_calls,
                    "unverified_action": runtime.unverified_action,
                },
            )
            return response
        except TimeoutError:
            runtime.stop_reason = "AGENT_RUNTIME_LIMIT"
            run.error = runtime.stop_reason
            run.final_summary = "Agent Run interrompido pelo limite total de runtime."
            await transition(AgentRunState.FAILED)
            await self.event_bus.publish(
                EventType.AGENT_RUN_FINISHED,
                agent_run_id=run.id, goal=run.goal, state=run.state.value,
                status=run.status.value, tool_calls=run.tool_calls, steps=len(run.steps),
                turn_id=run.turn_id, conversation_id=run.conversation_id,
            )
            return run.final_summary
        except asyncio.CancelledError:
            cancellation.set()
            run.error = "cancelled"
            await transition(AgentRunState.CANCELLED)
            await self.event_bus.publish(EventType.AGENT_RUN_CANCELLED, agent_run_id=run.id, goal=run.goal, turn_id=run.turn_id)
            raise
        finally:
            current_agent_run_id.reset(token)
            self._last_budget = {
                "run_id": run.id, **action_budget.snapshot(),
                "last_exceeded": getattr(action_budget, "last_exceeded", None),
            }
            self._budgets.pop(run.id, None)
            if self._tasks.get(run.id) is current_task:
                self._tasks.pop(run.id, None)
            for resource, owner in list(self._resource_owners.items()):
                if owner == run.id:
                    await release_resource(resource)

    async def cancel(self, run_id: str, reason: str = "operator_cancelled") -> bool:
        run = self._runs.get(run_id) or await self.store.get(run_id)
        if not run or run.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}:
            return False
        self._runs[run.id] = run
        self._cancellations.setdefault(run.id, asyncio.Event()).set()
        task = self._tasks.get(run.id)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        run.state = AgentRunState.CANCELLED
        run.status = AgentRunStatus.CANCELLED
        run.error = reason
        run.updated_at = datetime.now(timezone.utc)
        await self.store.save(run)
        await self.event_bus.publish(
            EventType.AGENT_RUN_CANCELLED,
            agent_run_id=run.id,
            goal=run.goal,
            reason=reason,
            turn_id=run.turn_id,
            conversation_id=run.conversation_id,
        )
        return True

    async def cancel_active(self, reason: str = "operator_cancelled") -> int:
        count = 0
        for run_id in list(self._runs):
            if await self.cancel(run_id, reason):
                count += 1
        return count

    async def approval_denied(self, run_id: str | None) -> None:
        if run_id:
            await self.cancel(run_id, "approval_denied")

    async def recent(self, limit: int = 30) -> list[AgentRun]:
        return await self.store.recent(limit)

    async def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id) or await self.store.get(run_id)

    async def _resume_or_create(
        self,
        goal: str,
        resume_run_id: str | None,
        *,
        turn_id: str | None = None,
        conversation_id: str | None = None,
    ) -> AgentRun:
        if resume_run_id:
            run = self._runs.get(resume_run_id) or await self.store.get(resume_run_id)
            if not run:
                raise ValueError("Agent run para aprovação não foi encontrado")
            if run.status != AgentRunStatus.WAITING_APPROVAL:
                raise ValueError("Agent run não está aguardando aprovação")
            run.status = AgentRunStatus.RUNNING
            run.state = AgentRunState.PLAN
            run.pending_approval_id = None
            run.updated_at = datetime.now(timezone.utc)
            self._cancellations[run.id] = asyncio.Event()
        else:
            run = AgentRun(
                id="run_" + uuid4().hex,
                goal=redact_secrets(goal.strip())[:1000],
                turn_id=turn_id,
                conversation_id=conversation_id,
            )
        self._runs[run.id] = run
        if turn_id:
            if len(self._runs_by_turn) > 200:
                for stale in list(self._runs_by_turn)[:-100]:
                    self._runs_by_turn.pop(stale, None)
            self._runs_by_turn[turn_id] = run.id
        await self.store.save(run)
        return run

    def last_run_for_turn(self, turn_id: str | None) -> str | None:
        return self._runs_by_turn.get(turn_id or "")
