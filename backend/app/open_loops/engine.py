"""Persistent, grounded Open Loops Engine.

The engine remembers unfinished work; it never executes that work. Mutating an
OpenLoop does not bypass tools, approvals, risk policy, credentials or action
budgets owned by the existing execution layers.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import aiosqlite

from app.events import Event, EventBus, EventType
from app.intelligence.memory import MemoryV2Service
from app.intelligence.models import MemoryKind, MemoryWrite, Sensitivity
from app.intelligence.storage import IntelligenceStore
from app.intelligence.trust import contains_secret
from app.open_loops.models import (
    ArtifactReference,
    Goal,
    GoalCreate,
    GoalState,
    OpenLoop,
    OpenLoopCreate,
    OpenLoopState,
    OpenLoopType,
    ResolutionEvidence,
    ResumeContext,
    utc_now,
)
from app.open_loops.policy import (
    consolidation_score,
    continuity_intent,
    is_pending_query,
    is_resume_query,
    is_status_query,
    normalize,
    subject_key,
    topic_terms,
)
from app.tools.redaction import redact_secrets


_NONTERMINAL = {
    OpenLoopState.OPEN, OpenLoopState.ACTIVE, OpenLoopState.WAITING,
    OpenLoopState.BLOCKED, OpenLoopState.STALE,
}
_ACTIONABLE = {OpenLoopState.OPEN, OpenLoopState.ACTIVE, OpenLoopState.BLOCKED}
_VALID_RESOLUTION_EVIDENCE = {
    "artifact_verified", "monitor_condition_reached", "operator_confirmation",
    "selfdev_post_validation", "task_effect_verified", "task_state_succeeded",
    "tool_effect_verified",
}
_TRANSITIONS: dict[OpenLoopState, set[OpenLoopState]] = {
    OpenLoopState.OPEN: set(OpenLoopState),
    OpenLoopState.ACTIVE: set(OpenLoopState),
    OpenLoopState.WAITING: set(OpenLoopState),
    OpenLoopState.BLOCKED: set(OpenLoopState),
    OpenLoopState.STALE: {
        OpenLoopState.STALE, OpenLoopState.OPEN, OpenLoopState.ACTIVE,
        OpenLoopState.RESOLVED, OpenLoopState.CANCELLED,
    },
    OpenLoopState.RESOLVED: {OpenLoopState.RESOLVED},
    OpenLoopState.CANCELLED: {OpenLoopState.CANCELLED},
}


class OpenLoopEngine:
    """SQLite-backed goals/open loops with deterministic structured bridges."""

    def __init__(self, store: IntelligenceStore, memory: MemoryV2Service,
                 event_bus: EventBus) -> None:
        self.store = store
        self.memory = memory
        self.event_bus = event_bus
        self._lock = asyncio.Lock()
        self._events: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)
        self._runner: asyncio.Task[None] | None = None
        self.dropped_events = 0
        self._started = False
        self._last_error: str | None = None

    async def initialize(self) -> None:
        if self._started:
            return
        await self.apply_stale_policy()
        await self.event_bus.subscribe(self.observe_event)
        self._runner = asyncio.create_task(self._event_loop(), name="kazumi-open-loops-events")
        self._started = True
        await self._publish_summary()

    async def stop(self) -> None:
        if self._started:
            await self.event_bus.unsubscribe(self.observe_event)
        if self._runner and not self._runner.done():
            try:
                await asyncio.wait_for(self._events.join(), timeout=5)
            except TimeoutError:
                pass
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        self._runner = None
        self._started = False

    # ---------------------------------------------------------------- goals

    async def create_goal(self, request: GoalCreate) -> Goal:
        self._reject_secret(request.title)
        safe = request.model_copy(update={
            "project": self._safe_text(request.project, 240),
            "provenance": self._safe_mapping(request.provenance),
        })
        now = utc_now()
        async with self._lock:
            goals = await self.list_goals(include_terminal=False)
            candidate = next((item for item in goals if consolidation_score(
                item.title, safe.title, left_project=item.project,
                right_project=safe.project,
            ) >= .82), None)
            if candidate:
                candidate.priority = max(candidate.priority, safe.priority)
                candidate.updated_at = candidate.last_touched_at = now
                candidate.provenance = {**candidate.provenance, **safe.provenance}
                await self._save_goal(candidate)
                return candidate
            goal = Goal(
                title=self._safe_text(safe.title, 240) or "Objetivo",
                project=safe.project, priority=safe.priority,
                provenance=safe.provenance, created_at=now,
                updated_at=now, last_touched_at=now,
            )
            await self._save_goal(goal)
            await self._history("goal", goal.id, None, goal.state.value, "created", {})
        await self._publish_summary()
        return goal

    async def get_goal(self, goal_id: str) -> Goal | None:
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute(
                "SELECT document FROM goals_v1 WHERE goal_id=?", (goal_id,),
            )).fetchone()
        return Goal.model_validate_json(row[0]) if row else None

    async def list_goals(self, *, include_terminal: bool = True,
                         limit: int = 200) -> list[Goal]:
        where = "" if include_terminal else "WHERE state IN ('ACTIVE','PAUSED','STALE')"
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute(
                f"SELECT document FROM goals_v1 {where} ORDER BY priority DESC, last_touched_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )).fetchall()
        return [Goal.model_validate_json(row[0]) for row in rows]

    # -------------------------------------------------------------- lifecycle

    async def create(self, request: OpenLoopCreate, *, actor: str = "operator") -> tuple[OpenLoop, bool]:
        if request.state in {OpenLoopState.RESOLVED, OpenLoopState.CANCELLED, OpenLoopState.STALE}:
            raise ValueError("OPEN_LOOP_INITIAL_STATE_INVALID")
        self._reject_secret(request.title)
        self._reject_secret(request.next_possible_action or "")
        goal_id = await self._resolve_goal(request.goal, request.title, request.related_project,
                                           request.priority, request.provenance)
        now = utc_now()
        safe = request.model_copy(deep=True, update={
            "title": self._safe_text(request.title, 240) or "Pendência",
            "goal": goal_id,
            "context": self._safe_mapping(request.context),
            "source_turn": self._safe_text(request.source_turn, 160),
            "waiting_for": self._safe_mapping(request.waiting_for) if request.waiting_for else None,
            "next_possible_action": self._safe_text(request.next_possible_action, 1000),
            "provenance": self._safe_mapping({**request.provenance, "actor": actor}),
            "related_project": self._safe_text(request.related_project, 240),
            "related_task": self._safe_ids(request.related_task),
            "related_monitor": self._safe_ids(request.related_monitor),
            "related_artifact": self._safe_artifacts(request.related_artifact),
        })
        dedup_key = subject_key(safe.title, safe.related_project)
        async with self._lock:
            candidates = await self.list(states=list(_NONTERMINAL), limit=500)
            duplicate = self._duplicate(candidates, safe, dedup_key)
            if duplicate:
                previous = duplicate.state
                duplicate.title = self._prefer_title(duplicate.title, safe.title)
                duplicate.type = self._prefer_type(duplicate.type, safe.type)
                duplicate.state = self._merge_state(duplicate.state, safe.state)
                duplicate.goal = duplicate.goal or goal_id
                duplicate.context = {**duplicate.context, **safe.context}
                duplicate.source_turn = safe.source_turn or duplicate.source_turn
                duplicate.priority = max(duplicate.priority, safe.priority)
                duplicate.related_task = self._merge_ids(duplicate.related_task, safe.related_task)
                duplicate.related_monitor = self._merge_ids(duplicate.related_monitor, safe.related_monitor)
                duplicate.related_artifact = self._merge_artifacts(duplicate.related_artifact, safe.related_artifact)
                duplicate.waiting_for = safe.waiting_for or duplicate.waiting_for
                duplicate.next_possible_action = safe.next_possible_action or duplicate.next_possible_action
                duplicate.provenance = {**duplicate.provenance, **safe.provenance, "consolidated": True}
                duplicate.updated_at = duplicate.last_touched_at = now
                await self._save_loop(duplicate)
                await self._history(
                    "open_loop", duplicate.id, previous.value, duplicate.state.value,
                    "deduplicated", {"dedup_key": dedup_key},
                )
                result, deduplicated = duplicate, True
            else:
                result = OpenLoop(
                    **safe.model_dump(), created_at=now, updated_at=now,
                    last_touched_at=now, dedup_key=dedup_key,
                )
                await self._save_loop(result)
                await self._history(
                    "open_loop", result.id, None, result.state.value,
                    "created", {"actor": actor},
                )
                deduplicated = False
        await self._sync_goal(goal_id)
        await self._publish_summary()
        await self._emit(EventType.OPEN_LOOP_CREATED if not deduplicated else EventType.OPEN_LOOP_STATE_CHANGED,
                         loop_id=result.id, state=result.state.value, deduplicated=deduplicated)
        return result, deduplicated

    async def get(self, loop_id: str) -> OpenLoop | None:
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute(
                "SELECT document FROM open_loops_v1 WHERE loop_id=?", (loop_id,),
            )).fetchone()
        return OpenLoop.model_validate_json(row[0]) if row else None

    async def list(self, *, states: list[OpenLoopState] | None = None,
                   project: str | None = None, limit: int = 200) -> list[OpenLoop]:
        filters: list[str] = []
        params: list[Any] = []
        if states:
            filters.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(item.value for item in states)
        if project is not None:
            filters.append("project=?")
            params.append(project)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(limit, 500)))
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute(
                f"SELECT document FROM open_loops_v1 {where} "
                "ORDER BY priority DESC, last_touched_at DESC LIMIT ?", params,
            )).fetchall()
        return [OpenLoop.model_validate_json(row[0]) for row in rows]

    async def transition(self, loop_id: str, state: OpenLoopState, *, reason: str,
                         evidence: ResolutionEvidence | None = None,
                         actor: str = "operator") -> OpenLoop:
        loop = await self.get(loop_id)
        if loop is None:
            raise KeyError("OPEN_LOOP_NOT_FOUND")
        if state not in _TRANSITIONS[loop.state]:
            raise ValueError("OPEN_LOOP_TRANSITION_INVALID")
        if evidence:
            evidence = evidence.model_copy(deep=True, update={
                "source": self._safe_text(evidence.source, 120) or "unknown",
                "reference_id": self._safe_text(evidence.reference_id, 160),
                "detail": self._safe_mapping(evidence.detail),
            })
        if state == OpenLoopState.RESOLVED:
            self._validate_resolution(evidence, actor=actor, loop=loop)
        previous = loop.state
        loop.state = state
        loop.updated_at = loop.last_touched_at = utc_now()
        if evidence:
            loop.resolution_evidence = [*loop.resolution_evidence, evidence][-20:]
        if state == OpenLoopState.BLOCKED:
            loop.context = {**loop.context, "blocker": self._safe_text(reason, 500)}
        async with self._lock:
            await self._save_loop(loop)
            await self._history(
                "open_loop", loop.id, previous.value, state.value,
                self._safe_text(reason, 500) or "transition", evidence.model_dump(mode="json") if evidence else {},
            )
        await self._sync_goal(loop.goal)
        if state == OpenLoopState.RESOLVED:
            await self._remember_resolution(loop)
            await self._emit(EventType.OPEN_LOOP_RESOLVED, loop_id=loop.id, state=state.value)
        else:
            await self._emit(EventType.OPEN_LOOP_STATE_CHANGED, loop_id=loop.id, state=state.value)
        await self._publish_summary()
        return loop

    async def cancel(self, loop_id: str, *, reason: str = "operator_cancelled") -> OpenLoop:
        return await self.transition(loop_id, OpenLoopState.CANCELLED, reason=reason, actor="operator")

    async def resolve(self, loop_id: str, evidence: ResolutionEvidence, *,
                      actor: str = "system") -> OpenLoop:
        return await self.transition(
            loop_id, OpenLoopState.RESOLVED, reason=evidence.kind,
            evidence=evidence, actor=actor,
        )

    async def touch(self, loop_id: str, *, context: dict[str, Any] | None = None,
                    next_possible_action: str | None = None) -> OpenLoop | None:
        loop = await self.get(loop_id)
        if not loop:
            return None
        loop.context = {**loop.context, **self._safe_mapping(context or {})}
        if next_possible_action:
            self._reject_secret(next_possible_action)
            loop.next_possible_action = self._safe_text(next_possible_action, 1000)
        loop.updated_at = loop.last_touched_at = utc_now()
        async with self._lock:
            await self._save_loop(loop)
        await self._publish_summary()
        return loop

    # --------------------------------------------------------------- queries

    async def get_actionable_loops(self, *, limit: int = 50) -> list[OpenLoop]:
        return await self.list(states=list(_ACTIONABLE), limit=limit)

    async def get_waiting_loops(self, *, limit: int = 50) -> list[OpenLoop]:
        return await self.list(states=[OpenLoopState.WAITING], limit=limit)

    async def get_recent_resolved(self, *, limit: int = 20) -> list[OpenLoop]:
        values = await self.list(states=[OpenLoopState.RESOLVED], limit=max(limit, 100))
        return sorted(values, key=lambda item: item.last_touched_at, reverse=True)[:limit]

    async def get_priority(self, *, limit: int = 20) -> list[OpenLoop]:
        values = await self.list(states=list(_NONTERMINAL), limit=100)
        return sorted(values, key=lambda item: (item.priority, item.last_touched_at), reverse=True)[:limit]

    async def find_relevant(self, query: str, *, project: str | None = None,
                            source_turn: str | None = None,
                            include_recent_resolved: bool = True) -> OpenLoop | None:
        states = list(_NONTERMINAL)
        if include_recent_resolved:
            states.extend((OpenLoopState.RESOLVED, OpenLoopState.CANCELLED))
        values = await self.list(states=states, limit=300)
        if not values:
            return None
        now = utc_now()
        query_terms = topic_terms(query)
        generic = is_pending_query(query) or is_resume_query(query) or not query_terms
        if generic:
            active_values = [item for item in values if item.state in _NONTERMINAL]
            if active_values:
                values = active_values

        def score(loop: OpenLoop) -> float:
            age_hours = max(0.0, (now - loop.last_touched_at).total_seconds() / 3600)
            recency = math.pow(.5, age_hours / (24 * 14))
            loop_terms = topic_terms(" ".join((
                loop.title, loop.related_project or "",
                str(loop.context.get("entity") or ""),
                " ".join(item.path for item in loop.related_artifact),
            )))
            lexical = len(query_terms & loop_terms) / max(1, len(query_terms))
            relation = 1.0 if source_turn and source_turn == loop.source_turn else 0.0
            project_score = 1.0 if project and normalize(project) == normalize(loop.related_project or "") else 0.0
            active_bonus = .12 if loop.state in _NONTERMINAL else 0.0
            generic_bonus = .20 if generic else 0.0
            return lexical * .48 + recency * .20 + project_score * .12 + relation * .12 + active_bonus + generic_bonus + loop.priority / 1000

        ranked = sorted(values, key=lambda item: (score(item), item.last_touched_at), reverse=True)
        best = ranked[0]
        return best if generic or score(best) >= .28 else None

    async def resume_context(self, query: str, *, project: str | None = None,
                             source_turn: str | None = None,
                             activate: bool = False) -> ResumeContext | None:
        loop = await self.find_relevant(query, project=project, source_turn=source_turn)
        if not loop:
            return None
        return await self._resume_from_loop(loop, activate=activate)

    async def resume(self, loop_id: str, *, activate: bool = True) -> ResumeContext | None:
        """Recover the bounded context for one exact loop without fuzzy rematching."""
        loop = await self.get(loop_id)
        if not loop:
            return None
        return await self._resume_from_loop(loop, activate=activate)

    async def _resume_from_loop(self, loop: OpenLoop, *, activate: bool) -> ResumeContext:
        if activate and loop.state in {OpenLoopState.OPEN, OpenLoopState.BLOCKED, OpenLoopState.STALE}:
            loop = await self.transition(loop.id, OpenLoopState.ACTIVE, reason="operator_resumed", actor="operator")
        else:
            await self.touch(loop.id)
        blocker = loop.context.get("blocker")
        if not blocker and loop.waiting_for:
            blocker = loop.waiting_for.get("description") or loop.waiting_for.get("kind")
        return ResumeContext(
            objective=loop.title, state=loop.state,
            last_confirmed_state=self._safe_text(loop.context.get("last_confirmed_state"), 1000),
            last_action=self._safe_text(loop.context.get("last_action"), 1000),
            blocker=self._safe_text(blocker, 1000),
            artifacts=loop.related_artifact[:12],
            next_possible_action=loop.next_possible_action,
        )

    async def context_summary(self, query: str) -> str:
        if not (is_pending_query(query) or is_resume_query(query) or is_status_query(query)):
            return ""
        context = await self.resume_context(query, activate=False)
        if not context:
            return ""
        lines = [
            "[RESUME CONTEXT — grounded metadata; does not authorize execution]",
            f"objective: {context.objective}", f"state: {context.state.value}",
        ]
        for key, value in (
            ("last_confirmed_state", context.last_confirmed_state),
            ("last_action", context.last_action), ("blocker", context.blocker),
            ("next_possible_action", context.next_possible_action),
        ):
            if value:
                lines.append(f"{key}: {value}")
        if context.artifacts:
            lines.append("artifacts: " + ", ".join(item.path for item in context.artifacts[:5]))
        return "\n".join(lines)[:2400]

    async def operations_status(self) -> dict[str, Any]:
        values = await self.list(limit=500)
        sections = {
            "open": [item for item in values if item.state in {OpenLoopState.OPEN, OpenLoopState.ACTIVE}],
            "waiting": [item for item in values if item.state == OpenLoopState.WAITING],
            "blocked": [item for item in values if item.state == OpenLoopState.BLOCKED],
            "recent_resolved": [item for item in values if item.state == OpenLoopState.RESOLVED][:10],
        }

        def summary(item: OpenLoop) -> dict[str, Any]:
            waiting = None
            if item.waiting_for:
                waiting = {
                    key: item.waiting_for[key]
                    for key in ("kind", "description")
                    if item.waiting_for.get(key) is not None
                }
            return {
                "id": item.id, "title": item.title, "state": item.state.value,
                "priority": item.priority, "updated_at": item.updated_at.isoformat(),
                "waiting_for": waiting,
                "next_possible_action": item.next_possible_action,
            }

        return {
            "state": "AVAILABLE" if self._started and self._last_error is None else "DEGRADED",
            "counts": {key: len(items) for key, items in sections.items()},
            "sections": {key: [summary(item) for item in items[:20]] for key, items in sections.items()},
            "last_error": self._last_error,
            "dropped_events": self.dropped_events,
        }

    async def chat_response(self, text: str, *, project: str | None = None,
                            source_turn: str | None = None) -> str | None:
        if is_pending_query(text):
            values = await self.list(states=[
                OpenLoopState.OPEN, OpenLoopState.ACTIVE,
                OpenLoopState.WAITING, OpenLoopState.BLOCKED,
            ], limit=20)
            if not values:
                return "Não encontrei nada pendente no momento."
            labels = [self._natural_label(item) for item in values[:5]]
            if len(labels) == 1:
                return f"Temos uma coisa em aberto: {labels[0]}."
            prefix = f"Temos {len(values)} coisas em aberto: "
            return prefix + "; ".join(labels) + ("." if len(values) <= 5 else "; entre outras.")
        if is_resume_query(text):
            context = await self.resume_context(text, project=project, source_turn=source_turn, activate=True)
            if not context:
                return "Não encontrei uma atividade anterior clara para retomar."
            details = [f"Vamos retomar {context.objective}"]
            if context.last_confirmed_state:
                details.append(f"o último estado confirmado era {context.last_confirmed_state}")
            if context.blocker:
                details.append(f"o bloqueio era {context.blocker}")
            if context.next_possible_action:
                details.append(f"o próximo passo possível é {context.next_possible_action}")
            return ". ".join(details) + ". Isso só recupera o contexto; qualquer ação continua sujeita às políticas normais."
        if is_status_query(text):
            loop = await self.find_relevant(text, project=project, source_turn=source_turn)
            if not loop:
                return None
            if loop.state == OpenLoopState.RESOLVED:
                return f"{loop.title} foi resolvido com evidência verificada."
            if loop.state == OpenLoopState.CANCELLED:
                return f"{loop.title} foi cancelado."
            return f"{loop.title} ainda está {self._state_pt(loop.state)}."
        return None

    async def observe_user_intention(self, text: str, *, source_turn: str | None,
                                     project: str | None = None) -> OpenLoop | None:
        intent = continuity_intent(text)
        if intent is None or contains_secret(text):
            return None
        kind, state = intent
        waiting_for = None
        next_action = "Retomar com confirmação do operador."
        if state == OpenLoopState.WAITING:
            waiting_for = {"kind": "external_condition", "description": self._safe_text(text, 500)}
            next_action = "Verificar a condição por uma fonte read-only ou MonitorJob vinculado."
        elif state == OpenLoopState.BLOCKED:
            waiting_for = {"kind": "blocker", "description": self._safe_text(text, 500)}
            next_action = "Remover o bloqueio e verificar o estado antes de continuar."
        loop, _ = await self.create(OpenLoopCreate(
            title=self._safe_text(text, 240) or "Pendência registrada",
            type=kind, state=state, goal=self._safe_text(text, 240),
            source_turn=source_turn, related_project=project,
            context={
                "last_confirmed_state": self._safe_text(text, 500),
                "resolve_on_condition": not bool(re.search(
                    r"(?i)\bpara\s+(?:continuar|retomar|fazer|configurar|executar)\b", text,
                )),
            },
            waiting_for=waiting_for, next_possible_action=next_action,
            provenance={"source": "operator_text", "evidence_level": "CONFIRMED"},
        ), actor="operator")
        return loop

    # --------------------------------------------------------- structured IO

    async def reconcile(self, *, tasks: Iterable[dict[str, Any]] = (),
                        monitors: Iterable[dict[str, Any]] = (),
                        artifacts: Iterable[dict[str, Any]] = ()) -> None:
        for task in tasks:
            await self._task_event("TASK_STATE_CHANGED", dict(task))
        for monitor in monitors:
            await self._monitor_event("MONITOR_JOB_CREATED", dict(monitor))
        for artifact in artifacts:
            await self._artifact_event({"artifact": dict(artifact), "verified": artifact.get("exists_state") in {"created", "verified"}})
        await self._publish_summary()

    async def observe_event(self, event: Event) -> None:
        name = str(getattr(event.type, "value", event.type))
        if name.startswith("OPEN_LOOP_"):
            return
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1

    async def _event_loop(self) -> None:
        while True:
            event = await self._events.get()
            try:
                await self._process_event(event)
            finally:
                self._events.task_done()

    async def _process_event(self, event: Event) -> None:
        name = str(getattr(event.type, "value", event.type))
        payload = event.payload if isinstance(event.payload, dict) else {}
        try:
            if event.type in {EventType.TASK_STARTED, EventType.TASK_STATE_CHANGED, EventType.TASK_FINISHED}:
                await self._task_event(name, payload)
            elif event.type in {
                EventType.AGENT_RUN_STARTED, EventType.AGENT_RUN_STATE_CHANGED,
                EventType.AGENT_RUN_FINISHED, EventType.AGENT_RUN_CANCELLED,
            }:
                await self._agent_event(name, payload)
            elif event.type in {
                EventType.MONITOR_JOB_CREATED, EventType.MONITOR_JOB_READING,
                EventType.MONITOR_JOB_CHANGED, EventType.MONITOR_JOB_COMPLETED,
                EventType.MONITOR_JOB_FAILED, EventType.MONITOR_JOB_CANCELLED,
            }:
                await self._monitor_event(name, payload)
            elif event.type == EventType.ARTIFACT_CONTEXT_UPDATED:
                await self._artifact_event(payload)
            elif name.startswith("selfdev."):
                await self._selfdev_event(name, payload)
        except Exception as error:  # persistence issues degrade without breaking publishers
            self._last_error = type(error).__name__

    async def _task_event(self, name: str, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("task_id") or "")[:160]
        if not task_id:
            return
        state = str(getattr(payload.get("state"), "value", payload.get("state") or "")).upper()
        loop = await self._find_relation(task_id=task_id)
        title = str(payload.get("objective") or payload.get("goal") or payload.get("title") or "")[:240]
        if not loop and state not in {"COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED"}:
            loop, _ = await self.create(OpenLoopCreate(
                title=title or f"Task {task_id}", type=OpenLoopType.GOAL,
                state=self._task_loop_state(state), goal=str(payload.get("goal_id") or title or task_id),
                source_turn=payload.get("source_turn"), related_task=[task_id],
                next_possible_action="Acompanhar o Task Engine; não executar fora da política da task.",
                provenance={"source": str(payload.get("source") or "task_engine"), "structured": True},
            ), actor="task_engine")
        if not loop:
            return
        if state in {"COMPLETED", "SUCCEEDED"}:
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            verified = state == "SUCCEEDED" or result.get("effect_verified") is True
            if verified:
                await self.resolve(loop.id, ResolutionEvidence(
                    kind="task_effect_verified" if result else "task_state_succeeded",
                    source=str(payload.get("source") or "task_engine"), verified=True,
                    reference_id=task_id, detail={"state": state},
                ))
        elif state == "CANCELLED":
            await self.cancel(loop.id, reason="linked_task_cancelled")
        elif state == "FAILED":
            await self.transition(loop.id, OpenLoopState.BLOCKED, reason="linked_task_failed", actor="task_engine")
        elif loop.state != self._task_loop_state(state):
            await self.transition(loop.id, self._task_loop_state(state), reason=f"linked_task_{state.casefold()}", actor="task_engine")

    async def _agent_event(self, name: str, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("agent_run_id") or "")[:160]
        if not run_id:
            return
        loop = await self._find_relation(task_id=run_id)
        goal = str(payload.get("goal") or f"Agent Run {run_id}")[:240]
        if not loop and name != "AGENT_RUN_CANCELLED":
            loop, _ = await self.create(OpenLoopCreate(
                title=goal, type=OpenLoopType.GOAL, state=OpenLoopState.ACTIVE,
                goal=goal, source_turn=payload.get("turn_id"), related_task=[run_id],
                context={"last_action": "Agent Run iniciado"},
                next_possible_action="Retomar somente pelo Agent Run e suas políticas de approval.",
                provenance={"source": "agent_controller", "structured": True},
            ), actor="agent_controller")
        if not loop:
            return
        state = str(payload.get("state") or payload.get("status") or "").upper()
        if name == "AGENT_RUN_CANCELLED":
            reason = str(payload.get("reason") or "interrupted")
            if reason in {"operator_voice_or_chat", "operator_cancelled", "approval_denied"}:
                await self.cancel(loop.id, reason=reason)
            else:
                await self.transition(loop.id, OpenLoopState.OPEN, reason="agent_run_interrupted", actor="agent_controller")
        elif state in {"WAITING_APPROVAL", "FAILED"}:
            await self.transition(loop.id, OpenLoopState.BLOCKED, reason=f"agent_run_{state.casefold()}", actor="agent_controller")
        elif name == "AGENT_RUN_FINISHED" and state in {"COMPLETE", "COMPLETED"}:
            if payload.get("unverified_action") is not True:
                await self.resolve(loop.id, ResolutionEvidence(
                    kind="task_effect_verified", source="agent_controller", verified=True,
                    reference_id=run_id, detail={"status": payload.get("status")},
                ))
        elif state in {"RUNNING", "VERIFYING"} and loop.state != OpenLoopState.ACTIVE:
            await self.transition(loop.id, OpenLoopState.ACTIVE, reason="agent_run_active", actor="agent_controller")

    async def _monitor_event(self, name: str, payload: dict[str, Any]) -> None:
        monitor_id = str(payload.get("monitor_id") or "")[:160]
        if not monitor_id:
            return
        loop = await self._find_relation(monitor_id=monitor_id)
        objective = str(payload.get("objective") or f"Monitor {monitor_id}")[:240]
        if not loop and name == "MONITOR_JOB_CREATED":
            loop, _ = await self.create(OpenLoopCreate(
                title=objective, type=OpenLoopType.WAITING_CONDITION,
                state=OpenLoopState.WAITING, goal=objective,
                source_turn=payload.get("source_turn_id"), related_monitor=[monitor_id],
                waiting_for={"kind": "monitor_condition", "condition": payload.get("condition"),
                             "description": objective},
                context={"last_confirmed_state": self._monitor_reading(payload),
                         "resolve_on_condition": True},
                next_possible_action="Aguardar o MonitorJob; a condição não autoriza outra ação.",
                provenance={"source": "monitor_job", "structured": True},
            ), actor="monitor_job")
        if not loop:
            return
        if name in {"MONITOR_JOB_READING", "MONITOR_JOB_CHANGED"}:
            await self.touch(loop.id, context={"last_confirmed_state": self._monitor_reading(payload)})
        elif name == "MONITOR_JOB_COMPLETED":
            reading = payload.get("last_reading") if isinstance(payload.get("last_reading"), dict) else {}
            if payload.get("completion_reason") == "CONDITION_MET" and reading.get("ok") is True:
                evidence = ResolutionEvidence(
                    kind="monitor_condition_reached", source="monitor_job", verified=True,
                    reference_id=monitor_id,
                    detail={"completion_reason": "CONDITION_MET", "observed_at": reading.get("observed_at")},
                )
                if loop.context.get("resolve_on_condition", True):
                    await self.resolve(loop.id, evidence)
                else:
                    await self.touch(
                        loop.id,
                        context={"last_confirmed_state": "A condição aguardada foi atingida."},
                    )
                    await self.transition(
                        loop.id, OpenLoopState.ACTIVE,
                        reason="monitor_condition_reached_next_action_pending",
                        evidence=evidence, actor="monitor_job",
                    )
            else:
                await self.transition(loop.id, OpenLoopState.BLOCKED, reason="monitor_ended_without_condition", actor="monitor_job")
        elif name == "MONITOR_JOB_FAILED":
            await self.transition(loop.id, OpenLoopState.BLOCKED, reason="monitor_probe_failed", actor="monitor_job")
        elif name == "MONITOR_JOB_CANCELLED":
            await self.cancel(loop.id, reason="linked_monitor_cancelled")

    async def _artifact_event(self, payload: dict[str, Any]) -> None:
        if payload.get("verified") is not True or not isinstance(payload.get("artifact"), dict):
            return
        raw = payload["artifact"]
        path = str(raw.get("path") or "")[:1200]
        if not path:
            return
        references = self._safe_artifacts([ArtifactReference(
            artifact_id=str(raw.get("artifact_id") or "")[:120] or None,
            path=path, kind=str(raw.get("kind") or "file")[:60],
            exists_state=str(raw.get("exists_state") or "verified")[:40],
        )])
        if not references:
            return
        reference = references[0]
        path = reference.path
        loops = await self.list(states=list(_NONTERMINAL), limit=300)
        matches = [item for item in loops if (
            (raw.get("source_turn_id") and raw.get("source_turn_id") == item.source_turn)
            or any(existing.path.casefold() == path.casefold() for existing in item.related_artifact)
            or self._waiting_matches_artifact(item, path)
        )]
        for loop in matches:
            loop.related_artifact = self._merge_artifacts(loop.related_artifact, [reference])
            loop.context = {**loop.context, "last_confirmed_state": f"Artefato verificado: {path}"}
            loop.updated_at = loop.last_touched_at = utc_now()
            async with self._lock:
                await self._save_loop(loop)
            if self._waiting_matches_artifact(loop, path):
                await self.resolve(loop.id, ResolutionEvidence(
                    kind="artifact_verified", source="artifact_context", verified=True,
                    reference_id=reference.artifact_id or path,
                    detail={"path": path, "exists_state": reference.exists_state},
                ))
        if matches:
            await self._publish_summary()

    async def _selfdev_event(self, name: str, payload: dict[str, Any]) -> None:
        issue_id = str(payload.get("issue_id") or "")[:160]
        if not issue_id:
            return
        relation = f"selfdev:{issue_id}"
        loop = await self._find_relation(task_id=relation)
        if not loop and name == EventType.SELFDEV_ISSUE_DETECTED.value:
            title = str(payload.get("title") or f"SelfDev {issue_id}")[:240]
            loop, _ = await self.create(OpenLoopCreate(
                title=title, type=OpenLoopType.BLOCKED_WORK, state=OpenLoopState.OPEN,
                goal=title, related_task=[relation], related_project="KAZUMI",
                next_possible_action="Seguir o lifecycle SelfDev com validação e rollback.",
                provenance={"source": "selfdev", "structured": True, "issue_id": issue_id},
            ), actor="selfdev")
        if not loop:
            return
        if name == EventType.SELFDEV_POST_VALIDATION_PASS.value:
            await self.resolve(loop.id, ResolutionEvidence(
                kind="selfdev_post_validation", source="selfdev", verified=True,
                reference_id=issue_id,
            ))
        elif name in {EventType.SELFDEV_VALIDATION_FAIL.value, EventType.SELFDEV_ROLLBACK.value}:
            await self.transition(loop.id, OpenLoopState.BLOCKED, reason=name, actor="selfdev")
        elif name in {
            EventType.SELFDEV_PLAN_CREATED.value, EventType.SELFDEV_WORKTREE_CREATED.value,
            EventType.SELFDEV_PATCH_READY.value, EventType.SELFDEV_VALIDATION_PASS.value,
            EventType.SELFDEV_PROMOTION_APPLIED.value,
        } and loop.state != OpenLoopState.ACTIVE:
            await self.transition(loop.id, OpenLoopState.ACTIVE, reason=name, actor="selfdev")

    # -------------------------------------------------------------- staleness

    async def apply_stale_policy(self, *, now: datetime | None = None,
                                 stale_after_days: int = 90,
                                 closed_projects: Iterable[str] = (),
                                 superseded_goals: Iterable[str] = ()) -> int:
        current = now or utc_now()
        threshold = current - timedelta(days=max(1, stale_after_days))
        closed = {normalize(item) for item in closed_projects}
        superseded = set(superseded_goals)
        values = await self.list(states=[
            OpenLoopState.OPEN, OpenLoopState.ACTIVE,
            OpenLoopState.WAITING, OpenLoopState.BLOCKED,
        ], limit=500) if self.store.database_path.exists() else []
        stale: list[OpenLoop] = []
        for loop in values:
            reason = None
            if loop.last_touched_at < threshold:
                reason = "stale_by_time"
            elif loop.related_project and normalize(loop.related_project) in closed:
                reason = "project_closed"
            elif loop.goal and loop.goal in superseded:
                reason = "goal_superseded"
            if reason:
                previous = loop.state
                loop.state = OpenLoopState.STALE
                loop.updated_at = loop.last_touched_at = current
                await self._save_loop(loop)
                await self._history("open_loop", loop.id, previous.value, "STALE", reason, {})
                stale.append(loop)
        if stale:
            for goal_id in {item.goal for item in stale if item.goal}:
                await self._sync_goal(goal_id)
            await self._publish_summary()
        return len(stale)

    # --------------------------------------------------------------- storage

    async def _save_loop(self, loop: OpenLoop) -> None:
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                """INSERT INTO open_loops_v1(
                    loop_id,title,type,state,goal_id,project,dedup_key,source_turn,
                    priority,created_at,updated_at,last_touched_at,document
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(loop_id) DO UPDATE SET
                    title=excluded.title,type=excluded.type,state=excluded.state,
                    goal_id=excluded.goal_id,project=excluded.project,dedup_key=excluded.dedup_key,
                    source_turn=excluded.source_turn,priority=excluded.priority,
                    updated_at=excluded.updated_at,last_touched_at=excluded.last_touched_at,
                    document=excluded.document""",
                (loop.id, loop.title, loop.type.value, loop.state.value, loop.goal,
                 loop.related_project or "", loop.dedup_key, loop.source_turn,
                 loop.priority, loop.created_at.isoformat(), loop.updated_at.isoformat(),
                 loop.last_touched_at.isoformat(), loop.model_dump_json()),
            )
            await db.commit()

    async def _save_goal(self, goal: Goal) -> None:
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                """INSERT INTO goals_v1(goal_id,title,state,project,priority,created_at,updated_at,last_touched_at,document)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET
                title=excluded.title,state=excluded.state,project=excluded.project,
                priority=excluded.priority,updated_at=excluded.updated_at,
                last_touched_at=excluded.last_touched_at,document=excluded.document""",
                (goal.id, goal.title, goal.state.value, goal.project or "", goal.priority,
                 goal.created_at.isoformat(), goal.updated_at.isoformat(),
                 goal.last_touched_at.isoformat(), goal.model_dump_json()),
            )
            await db.commit()

    async def _history(self, entity_type: str, entity_id: str, previous: str | None,
                       current: str, reason: str, evidence: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                """INSERT INTO open_loop_history(entity_type,entity_id,previous_state,new_state,reason,evidence,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (entity_type, entity_id, previous, current, reason,
                 json.dumps(self._safe_mapping(evidence), ensure_ascii=False), utc_now().isoformat()),
            )
            await db.commit()

    # --------------------------------------------------------------- helpers

    async def _resolve_goal(self, value: str | None, fallback_title: str,
                            project: str | None, priority: int,
                            provenance: dict[str, Any]) -> str:
        if value and value.startswith("goal_"):
            existing = await self.get_goal(value)
            if existing:
                return existing.id
        title = value or fallback_title
        goal = await self.create_goal(GoalCreate(
            title=title, project=project, priority=priority,
            provenance={**provenance, "source": provenance.get("source", "open_loops_engine")},
        ))
        return goal.id

    async def _sync_goal(self, goal_id: str | None) -> None:
        if not goal_id:
            return
        goal = await self.get_goal(goal_id)
        if not goal:
            return
        loops = [item for item in await self.list(limit=500) if item.goal == goal_id]
        if not loops:
            return
        states = {item.state for item in loops}
        new_state = GoalState.ACTIVE
        if states <= {OpenLoopState.RESOLVED}:
            new_state = GoalState.RESOLVED
        elif states <= {OpenLoopState.CANCELLED}:
            new_state = GoalState.CANCELLED
        elif states <= {OpenLoopState.RESOLVED, OpenLoopState.CANCELLED}:
            new_state = GoalState.RESOLVED
        elif states <= {OpenLoopState.STALE}:
            new_state = GoalState.STALE
        previous = goal.state
        goal.state = new_state
        goal.updated_at = goal.last_touched_at = max(item.last_touched_at for item in loops)
        await self._save_goal(goal)
        if previous != new_state:
            await self._history("goal", goal.id, previous.value, new_state.value, "derived_from_open_loops", {})

    async def _find_relation(self, *, task_id: str | None = None,
                             monitor_id: str | None = None) -> OpenLoop | None:
        values = await self.list(states=list(_NONTERMINAL), limit=500)
        for item in values:
            if task_id and task_id in item.related_task:
                return item
            if monitor_id and monitor_id in item.related_monitor:
                return item
        return None

    def _duplicate(self, candidates: list[OpenLoop], request: OpenLoopCreate,
                   dedup_key: str) -> OpenLoop | None:
        for item in candidates:
            if request.source_turn and item.source_turn == request.source_turn:
                return item
            if request.related_task and set(request.related_task) & set(item.related_task):
                return item
            if request.related_monitor and set(request.related_monitor) & set(item.related_monitor):
                return item
            if request.related_artifact and {value.path.casefold() for value in request.related_artifact} & {
                value.path.casefold() for value in item.related_artifact
            }:
                return item
            # Explicit distinct goals are a consolidation boundary. Exact
            # Task/Monitor/Artifact/source-turn relations above remain
            # authoritative when two producers are describing the same work.
            if request.goal and item.goal and request.goal != item.goal:
                continue
            if item.dedup_key == dedup_key:
                return item
            if consolidation_score(
                item.title, request.title, left_project=item.related_project,
                right_project=request.related_project,
            ) >= .72:
                return item
        return None

    @staticmethod
    def _validate_resolution(evidence: ResolutionEvidence | None, *, actor: str,
                             loop: OpenLoop) -> None:
        if evidence is None or evidence.verified is not True:
            raise ValueError("OPEN_LOOP_RESOLUTION_EVIDENCE_REQUIRED")
        if evidence.kind not in _VALID_RESOLUTION_EVIDENCE:
            raise ValueError("OPEN_LOOP_RESOLUTION_EVIDENCE_INVALID")
        if actor == "operator" and evidence.kind != "operator_confirmation":
            raise ValueError("OPEN_LOOP_AUTOMATED_EVIDENCE_SERVER_OWNED")
        if actor != "operator" and evidence.kind == "operator_confirmation":
            raise ValueError("OPEN_LOOP_OPERATOR_CONFIRMATION_INVALID")
        if normalize(evidence.source).startswith(("llm", "assistant", "model", "prompt")):
            raise ValueError("OPEN_LOOP_LLM_CANNOT_RESOLVE")
        if evidence.kind == "operator_confirmation" and not normalize(evidence.source).startswith("operator"):
            raise ValueError("OPEN_LOOP_OPERATOR_CONFIRMATION_INVALID")
        if evidence.kind == "task_effect_verified" and loop.related_task and evidence.reference_id not in loop.related_task:
            raise ValueError("OPEN_LOOP_RESOLUTION_REFERENCE_MISMATCH")
        if evidence.kind == "monitor_condition_reached" and loop.related_monitor and evidence.reference_id not in loop.related_monitor:
            raise ValueError("OPEN_LOOP_RESOLUTION_REFERENCE_MISMATCH")

    async def _remember_resolution(self, loop: OpenLoop) -> None:
        summary = f"Open loop resolvido com evidência: {loop.title}"
        if contains_secret(summary):
            return
        try:
            await self.memory.write(MemoryWrite(
                kind=MemoryKind.EPISODIC, content=summary,
                source="open_loops_engine", category="open_loop_resolution",
                project=loop.related_project, confidence=1.0, relevance=.85,
                sensitivity=Sensitivity.INTERNAL,
                provenance={"source": "open_loops_engine", "loop_id": loop.id,
                            "evidence": [item.kind for item in loop.resolution_evidence]},
                related_entities=[item for item in [loop.goal, *loop.related_task, *loop.related_monitor] if item],
            ), force=True)
        except (PermissionError, ValueError):
            return

    async def _publish_summary(self) -> None:
        try:
            values = await self.list(limit=500)
            active = [item for item in values if item.state in {
                OpenLoopState.OPEN, OpenLoopState.ACTIVE,
                OpenLoopState.WAITING, OpenLoopState.BLOCKED,
            }]
            waiting = [item for item in active if item.state == OpenLoopState.WAITING]
            most = sorted(active, key=lambda item: (item.priority, item.last_touched_at), reverse=True)
            top = most[0] if most else None
            active_goal = await self.get_goal(top.goal) if top and top.goal else None
            await self._emit(
                EventType.OPEN_LOOP_SUMMARY_UPDATED,
                active_goal=active_goal.title if active_goal else None,
                open_loop_count=len(active), waiting_loop_count=len(waiting),
                most_relevant_open_loop=(
                    {"id": top.id, "title": top.title, "state": top.state.value,
                     "priority": top.priority, "project": top.related_project}
                    if top else None
                ), source="open_loops_engine",
            )
            self._last_error = None
        except Exception as error:
            self._last_error = type(error).__name__

    async def _emit(self, event_type: EventType, **payload: Any) -> None:
        await self.event_bus.publish(event_type, **payload)

    @staticmethod
    def _task_loop_state(state: str) -> OpenLoopState:
        if state in {"RUNNING", "VERIFYING"}:
            return OpenLoopState.ACTIVE
        if state in {"WAITING", "WAITING_FOR_JOB"}:
            return OpenLoopState.WAITING
        if state in {"WAITING_APPROVAL", "WAITING_FOR_USER", "FAILED"}:
            return OpenLoopState.BLOCKED
        return OpenLoopState.OPEN

    @staticmethod
    def _monitor_reading(payload: dict[str, Any]) -> str | None:
        reading = payload.get("last_reading") if isinstance(payload.get("last_reading"), dict) else payload
        summary = reading.get("summary") if isinstance(reading, dict) else None
        value = reading.get("value") if isinstance(reading, dict) else None
        return str(summary or (f"value={value}" if value is not None else ""))[:1000] or None

    @staticmethod
    def _waiting_matches_artifact(loop: OpenLoop, path: str) -> bool:
        waiting = loop.waiting_for or {}
        expected = str(waiting.get("path") or "")
        return waiting.get("kind") == "artifact_exists" and bool(expected) and expected.casefold() == path.casefold()

    @staticmethod
    def _merge_state(left: OpenLoopState, right: OpenLoopState) -> OpenLoopState:
        rank = {
            OpenLoopState.OPEN: 0, OpenLoopState.ACTIVE: 1, OpenLoopState.WAITING: 2,
            OpenLoopState.BLOCKED: 3, OpenLoopState.STALE: -1,
            OpenLoopState.RESOLVED: -2, OpenLoopState.CANCELLED: -3,
        }
        return right if rank[right] > rank[left] else left

    @staticmethod
    def _prefer_type(left: OpenLoopType, right: OpenLoopType) -> OpenLoopType:
        if left == OpenLoopType.GENERAL:
            return right
        return left

    @staticmethod
    def _prefer_title(left: str, right: str) -> str:
        return right if len(topic_terms(right)) > len(topic_terms(left)) else left

    @staticmethod
    def _merge_ids(left: list[str], right: list[str]) -> list[str]:
        return list(dict.fromkeys([*left, *right]))[:50]

    @staticmethod
    def _merge_artifacts(left: list[ArtifactReference], right: list[ArtifactReference]) -> list[ArtifactReference]:
        values: dict[str, ArtifactReference] = {item.path.casefold(): item for item in left}
        for item in right:
            values[item.path.casefold()] = item
        return list(values.values())[-50:]

    def _safe_artifacts(self, values: list[ArtifactReference]) -> list[ArtifactReference]:
        result: list[ArtifactReference] = []
        for item in values:
            if contains_secret(item.path):
                continue
            path = self._safe_text(item.path, 1200)
            if not path:
                continue
            path = re.sub(
                r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@",
                r"\1[REDACTED]@",
                path,
            )
            result.append(item.model_copy(update={
                "artifact_id": self._safe_text(item.artifact_id, 120),
                "path": path,
                "kind": self._safe_text(item.kind, 60) or "file",
                "exists_state": self._safe_text(item.exists_state, 40) or "unknown",
            }))
        return result

    def _safe_ids(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            raw = str(item).strip()
            if not raw or contains_secret(raw):
                continue
            clean = self._safe_text(raw, 160)
            if clean and clean not in result:
                result.append(clean)
        return result[:50]

    @staticmethod
    def _safe_text(value: Any, limit: int) -> str | None:
        if value is None:
            return None
        clean = " ".join(redact_secrets(str(value).replace("\x00", "")).split())[:limit]
        return clean or None

    @staticmethod
    def _safe_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
        try:
            rendered = json.dumps(value or {}, ensure_ascii=False, default=str)
            redacted = redact_secrets(rendered)[:12_000]
            parsed = json.loads(redacted)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _reject_secret(value: str) -> None:
        if value and contains_secret(value):
            raise PermissionError("OPEN_LOOP_SECRET_REJECTED")

    @staticmethod
    def _state_pt(state: OpenLoopState) -> str:
        return {
            OpenLoopState.OPEN: "em aberto", OpenLoopState.ACTIVE: "ativo",
            OpenLoopState.WAITING: "aguardando uma condição",
            OpenLoopState.BLOCKED: "bloqueado", OpenLoopState.STALE: "antigo e sem atividade recente",
            OpenLoopState.RESOLVED: "resolvido", OpenLoopState.CANCELLED: "cancelado",
        }[state]

    def _natural_label(self, item: OpenLoop) -> str:
        if item.state == OpenLoopState.WAITING:
            return f"aguardar {item.title}"
        if item.state == OpenLoopState.BLOCKED:
            return f"{item.title}, que está bloqueado"
        return item.title
