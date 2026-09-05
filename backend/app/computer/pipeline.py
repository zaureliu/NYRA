"""FASE D — Pipeline unificado (nyra-7c §75) sobre as 7 camadas.

User Input → Normalize/Intent → Skill Memory check → Universal Operator
→ Effect Verification → Computer State update → Usage Learning →
Skill candidate update → Grounded Response.

O RealtimeOrchestrator chama APENAS `handle_user_request`; se esta fachada
não tratar, o fluxo legado (LLM/Agent Loop) segue intacto. Nenhuma camada
nova duplica execução: quem executa ações físicas continua sendo o
DesktopController (ONE ACTION OWNER, §34).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nyra.computer.pipeline")


@dataclass
class HandleResult:
    handled: bool = False
    reply: str = ""
    intent_action: str = ""
    target: str = ""
    verified: bool | None = None
    skill_report: dict[str, Any] | None = None
    metrics: dict[str, float] = field(default_factory=dict)


class ComputerAutonomyService:
    """Fachada das camadas 1-7 para um turno de conversa."""

    def __init__(self, *, state, intent_service, perception, verifier, usage,
                 skills, desktop=None, remote_shell=None, artifacts=None,
                 clock=time.time) -> None:
        self.state = state
        self.intents = intent_service
        self.perception = perception
        self.verifier = verifier
        self.usage = usage
        self.skills = skills
        self.desktop = desktop
        if artifacts is None:
            from app.computer.artifacts import ArtifactContextService

            artifacts = ArtifactContextService(
                desktop=desktop, remote_shell=remote_shell, state=state,
            )
        self.artifacts = artifacts
        self.clock = clock
        self.metrics: dict[str, float] = {}
        self.failure_metrics: dict[str, int] = {
            "perception_failure": 0,
            "intent_resolution_failure": 0,
            "context_resolution_failure": 0,
            "operator_failure": 0,
            "verification_failure": 0,
            "usage_pattern_failure": 0,
            "skill_execution_failure": 0,
        }
        self.event_bus = getattr(skills, "event_bus", None) or \
            getattr(perception, "event_bus", None)
        self._last_resolutions: dict[str, dict[str, str]] = {}
        self._compound_executor = None

    # ------------------------------------------------------------- startup

    async def start_background(self) -> None:
        if self.perception is not None:
            await self.perception.start()

    async def shutdown(self) -> None:
        if self.perception is not None:
            await self.perception.stop()
        try:
            if self.state is not None and hasattr(self.state, "save_context"):
                self.state.save_context()
        finally:
            if self.artifacts is not None:
                self.artifacts.persist()
            for service in (self.usage, self.skills):
                if service is not None and hasattr(service, "persist"):
                    service.persist()

    def can_handle_without_llm(self, text: str, *, conversation_id: str = "default",
                               turn_id: str | None = None,
                               channel: str = "text") -> bool:
        """Preflight puro usado pela API durante warmup/offline do LLM."""
        if self._correction_target(text) and \
                (conversation_id or "default") in self._last_resolutions:
            return True
        if self.artifacts is not None:
            from app.computer.artifacts import parse_artifact_request

            if parse_artifact_request(text) is not None:
                return True
        if self.intents.resolve(text, conversation_id=conversation_id,
                                turn_id=turn_id, channel=channel) is not None:
            return True
        return bool(self.skills and self.skills.match(text))

    # -------------------------------------------------------------- turno

    async def handle_user_request(self, text: str, *, conversation_id: str = "default",
                                  turn_id: str | None = None,
                                  channel: str = "text") -> HandleResult:
        started = time.perf_counter()
        result = HandleResult()
        phase = {
            "context_resolve_ms": 0.0,
            "skill_lookup_ms": 0.0,
            "planning_ms": 0.0,
            "execution_ms": 0.0,
            "verification_ms": 0.0,
        }
        if self.intents is None:
            return result

        if self.artifacts is not None:
            artifact_result = await self.artifacts.try_handle(
                text, conversation_id=conversation_id, turn_id=turn_id,
            )
            if artifact_result is not None:
                result.handled = artifact_result.handled
                result.reply = artifact_result.reply
                result.intent_action = artifact_result.action
                result.target = (
                    artifact_result.artifact.path
                    if artifact_result.artifact is not None
                    else ""
                )
                result.verified = artifact_result.verified
                phase["context_resolve_ms"] = round(
                    (time.perf_counter() - started) * 1000, 2,
                )
                phase["artifact_reference_resolved"] = (
                    1.0 if artifact_result.artifact is not None else 0.0
                )
                phase["app_resolver_called"] = 0.0
                phase["agent_run_calls"] = float(
                    artifact_result.agent_run_calls,
                )
                self._finish(
                    result, None, started, conversation_id,
                    turn_id, phase,
                )
                return result
        if self.desktop is None:
            return result

        correction = self._correction_target(text)
        previous = self._last_resolutions.get(conversation_id or "default")
        if correction and previous and self.usage is not None:
            stat = self.usage.learn_alias_correction(
                previous["alias"], correction, kind=previous["kind"])
            from app.computer.usage import UsageEvent

            self.usage.record(UsageEvent(
                context_signature=self.usage.context_signature(
                    self._foreground_process(), channel),
                intent="USER_CORRECTION", target=correction[:80],
                verified_result=None, user_correction=True,
                confidence=stat.confidence,
            ))
            result.handled = True
            result.intent_action = "USER_CORRECTION"
            result.target = correction
            result.reply = (
                f"Entendi: “{previous['alias']}” se refere a {correction}. "
                "Vou exigir confirmações verificadas antes de usar esse alias automaticamente."
            )
            self._finish(result, None, started, conversation_id, turn_id, phase)
            return result

        intent_started = time.perf_counter()
        intent = self.intents.resolve(text, conversation_id=conversation_id,
                                      turn_id=turn_id, channel=channel)
        phase["context_resolve_ms"] = self.intents.metrics.get(
            "context_resolve_ms", 0.0)
        if intent is None:
            # Sem intenção determinística: tenta SKILL aprendida (§75).
            skill_started = time.perf_counter()
            matched = self.skills.match(text) if self.skills else None
            phase["skill_lookup_ms"] = round(
                (time.perf_counter() - skill_started) * 1000, 2)
            if matched is None:
                reason = getattr(self.intents, "last_failure_reason", None)
                if reason == "context_unresolved":
                    await self._record_failure(
                        "context_resolution_failure", turn_id=turn_id,
                        stage="intent", recoverable=True)
                elif reason == "unrecognized":
                    await self._record_failure(
                        "intent_resolution_failure", turn_id=turn_id,
                        stage="intent", recoverable=True)
                return result
            skill, matched_by = matched
            execution_started = time.perf_counter()
            report = await self.skills.execute(skill, controller=self.desktop,
                                               verifier=self.verifier, turn_id=turn_id)
            phase["execution_ms"] = round(
                (time.perf_counter() - execution_started) * 1000, 2)
            report.matched_by = matched_by
            result.handled = True
            result.reply = report.message or ("Skill executada." if report.ok
                                              else "Skill falhou; fallback aplicado.")
            result.intent_action = "SKILL_RUN"
            result.target = skill.name
            result.verified = report.ok
            result.skill_report = report.model_dump()
            self._record_skill_usage(skill, report)
            await self._publish("SKILL_EXECUTED", skill_id=skill.skill_id,
                                verified=report.ok, turn_id=turn_id)
            if not report.ok:
                await self._record_failure(
                    "skill_execution_failure", turn_id=turn_id,
                    stage="skill", recoverable=True, skill_id=skill.skill_id)
            self._finish(result, None, started, conversation_id, turn_id, phase)
            return result

        del intent_started
        self._apply_learned_alias(intent)
        result.intent_action = intent.action
        result.target = intent.target
        await self._publish("COMPUTER_INTENT_RESOLVED", intent_id=intent.intent_id,
                            action=intent.action, target=intent.target[:80],
                            confidence=intent.confidence, turn_id=turn_id)

        if intent.action == "PLAN":
            planning_started = time.perf_counter()
            # O plano já é determinístico/estruturado; esta métrica mede sua
            # preparação, sem incluir a execução física.
            phase["planning_ms"] = round(
                (time.perf_counter() - planning_started) * 1000, 2)
            execution_started = time.perf_counter()
            try:
                plan_result = await self._run_plan(intent)
            except Exception as error:  # noqa: BLE001 - deterministic owner must not leak to Agent Run
                logger.warning(
                    "computer_plan_failed type=%s", type(error).__name__,
                )
                plan_result = {
                    "success": False,
                    "effect_verified": False,
                    "message": f"Não consegui concluir a ação no {intent.target}.",
                    "steps": [{
                        "step": "compound_executor",
                        "ok": False,
                        "error_type": type(error).__name__,
                    }],
                    "app": intent.target,
                    "remote_shell_calls": 0,
                    "agent_run_calls": 0,
                }
            phase["execution_ms"] = round(
                (time.perf_counter() - execution_started) * 1000, 2)
            result.handled = True
            result.reply = str(plan_result.get("message") or "")
            raw_verified = plan_result.get("effect_verified")
            result.verified = None if raw_verified is None else bool(raw_verified)
            from app.computer.verification import VerifiedEffect

            effect = VerifiedEffect(
                expected=intent.desired_result,
                observed=("; ".join(
                    f"{step.get('step')}={bool(step.get('ok'))}"
                    for step in plan_result.get("steps", []))[:240]),
                source="filesystem+win32+uia",
                verified=result.verified,
                confidence=1.0 if result.verified else 0.4,
                details={
                    "file": plan_result.get("file"),
                    "content_match": plan_result.get("content_match"),
                    "close_verified": plan_result.get("close_verified"),
                },
            )
            self._note_plan_state(intent, plan_result, result.verified)
            try:
                self._learn(intent, effect, started, conversation_id, channel)
            except Exception as error:  # noqa: BLE001 — aprendizado não derruba a ação
                logger.warning("computer_usage_learning_failed type=%s", type(error).__name__)
                await self._record_failure(
                    "usage_pattern_failure", turn_id=turn_id,
                    stage="usage", recoverable=True,
                    error_type=type(error).__name__)
            self._remember_resolution(intent, conversation_id, plan_result)
            await self._publish("COMPUTER_ACTION_EXECUTED", action=intent.action,
                                target=intent.target[:80], turn_id=turn_id)
            await self._publish("COMPUTER_EFFECT_VERIFIED", action_id=effect.action_id,
                                verified=effect.verified, source=effect.source,
                                turn_id=turn_id)
            await self._publish("COMPUTER_STATE_UPDATED", action=intent.action,
                                target=intent.target[:80],
                                verified=result.verified, turn_id=turn_id)
            if result.verified is not True:
                if not plan_result.get("success", result.verified):
                    await self._record_failure(
                        "operator_failure", turn_id=turn_id,
                        stage="plan", recoverable=True, action=intent.action)
                await self._record_failure(
                    "verification_failure", turn_id=turn_id,
                    stage="verification", recoverable=True,
                    action=intent.action,
                    status="UNKNOWN" if result.verified is None else "FAILED")
            self._finish(result, intent, started, conversation_id, turn_id, phase)
            return result

        adapter = self._adapter_for(intent)
        if adapter is None:
            await self._record_failure(
                "intent_resolution_failure", turn_id=turn_id,
                stage="adapter", recoverable=False, action=intent.action)
            return result
        execution_started = time.perf_counter()
        handled, reply = await self.desktop.handle_universal(adapter, turn_id=turn_id)
        phase["execution_ms"] = round(
            (time.perf_counter() - execution_started) * 1000, 2)
        result.handled = bool(handled)
        result.reply = reply

        operation = getattr(self.desktop, "last_operation_result", None)
        effect = None
        if operation:
            verification_started = time.perf_counter()
            effect = self.verifier.from_operation_result(
                operation, expected=intent.desired_result or intent.action)
            phase["verification_ms"] = round(
                (time.perf_counter() - verification_started) * 1000, 2)
            result.verified = effect.verified
            # Presentation is a distinct boundary: the raw operation remains
            # available for verification/diagnostics, while only this sentence
            # reaches the assistant response and TTS pipeline.
            from app.desktop.presenter import ActionResultPresenter

            user_facing = ActionResultPresenter.present(
                operation,
                requested_action=intent.action,
                requested_app=intent.target,
            )
            if user_facing:
                operation["user_facing_response"] = user_facing
                result.reply = user_facing
        self._note_state(intent, result.verified)
        try:
            self._learn(intent, effect, started, conversation_id, channel)
        except Exception as error:  # noqa: BLE001 — aprendizado não derruba a ação
            logger.warning("computer_usage_learning_failed type=%s", type(error).__name__)
            await self._record_failure(
                "usage_pattern_failure", turn_id=turn_id,
                stage="usage", recoverable=True,
                error_type=type(error).__name__)
        self._remember_resolution(intent, conversation_id, operation or {})
        await self._publish("COMPUTER_ACTION_EXECUTED", action=intent.action,
                            target=intent.target[:80], turn_id=turn_id)
        if effect is not None:
            await self._publish("COMPUTER_EFFECT_VERIFIED", action_id=effect.action_id,
                                verified=effect.verified, source=effect.source,
                                turn_id=turn_id)
        if not handled:
            await self._record_failure(
                "operator_failure", turn_id=turn_id,
                stage="operator", recoverable=True, action=intent.action)
        if effect is None or effect.verified is not True:
            await self._record_failure(
                "verification_failure", turn_id=turn_id,
                stage="verification", recoverable=True,
                action=intent.action,
                status="UNKNOWN" if effect is None or effect.verified is None else "FAILED")
        await self._publish("COMPUTER_STATE_UPDATED", action=intent.action,
                            target=intent.target[:80],
                            verified=result.verified, turn_id=turn_id)
        self._finish(result, intent, started, conversation_id, turn_id, phase)
        return result

    # ------------------------------------------------------------- helpers

    async def _run_plan(self, intent) -> dict[str, Any]:
        if intent.arguments.get("plan_kind") == "compound_app":
            from app.desktop.compound import CompoundActionExecutor

            if self._compound_executor is None:
                self._compound_executor = CompoundActionExecutor(self.desktop)
            return await self._compound_executor.execute(
                intent, turn_id=intent.turn_id or None,
            )
        from app.desktop.multistep import notepad_write_and_save

        return await notepad_write_and_save(
            self.desktop,
            intent.arguments.get("text", ""),
            intent.arguments.get("filename", "documento.txt"),
            close_after=intent.arguments.get("close_after") == "true",
        )

    def _apply_learned_alias(self, intent) -> None:
        """Resolve alias operacional confirmado antes de escolher capability."""
        if self.usage is None or intent.requires_context:
            return
        alias = (intent.raw_target or intent.target).strip()
        if not alias:
            return
        kinds = {
            "OPEN_FOLDER": ("folder",),
            "OPEN_FILE": ("file",),
            "OPEN_APP": ("app", "folder", "file"),
        }.get(intent.action, ("app",))
        for kind in kinds:
            canonical = self.usage.resolve_alias(alias, kind=kind)
            if canonical is None:
                continue
            intent.arguments["learned_alias"] = alias
            intent.target = canonical
            intent.raw_target = canonical
            if intent.action == "OPEN_APP" and kind in {"folder", "file"}:
                intent.action = "OPEN_FOLDER" if kind == "folder" else "OPEN_FILE"
                intent.raw_action = intent.action
                intent.desired_result = (
                    "pasta aberta no Explorador" if kind == "folder"
                    else "arquivo aberto no aplicativo associado")
            return

    def _adapter_for(self, intent):
        from app.desktop.intents import UniversalAction, UniversalIntent

        try:
            action = UniversalAction(intent.raw_action)
        except ValueError:
            return None
        return UniversalIntent(
            action=action,
            target=intent.raw_target or intent.target,
            contextual=intent.raw_contextual,
            explicit_new=intent.raw_explicit_new,
        )

    def _note_state(self, intent, verified: bool | None) -> None:
        kind_map = {
            "OPEN_APP": "app", "CLOSE_APP": "app", "MINIMIZE_APP": "window",
            "MAXIMIZE_APP": "window", "RESTORE_APP": "window",
            "FOCUS_APP": "window", "SWITCH_APP": "window",
            "OPEN_FOLDER": "folder", "OPEN_FILE": "file",
        }
        kind = kind_map.get(intent.action, "app")
        process_names: tuple[str, ...] = ()
        hwnd: int | None = None
        title_tokens: tuple[str, ...] = (intent.target.casefold(),)
        operation = getattr(self.desktop, "last_operation_result", None)
        if operation and isinstance(operation.get("windows"), list) and operation["windows"]:
            first = operation["windows"][0]
            proc = str(first.get("process_name") or first.get("process") or "")
            proc = proc.casefold().removesuffix(".exe")
            if proc:
                process_names = (proc,)
            hwnd = int(first.get("hwnd") or 0) or None
        path = intent.resolved.path if intent.resolved else None
        canonical = self._canonical_target(intent, operation or {})
        self.state.note_action(
            action=intent.action, kind=kind, display_name=canonical,
            verified=bool(verified), conversation_id=intent.conversation_id,
            turn_id=intent.turn_id or None,
            process_names=process_names, title_tokens=title_tokens, path=path,
            hwnd=hwnd,
        )

    def _note_plan_state(self, intent, plan_result: dict[str, Any],
                         verified: bool | None) -> None:
        if intent.arguments.get("plan_kind") == "compound_app":
            display_name = str(plan_result.get("app") or intent.target)
            process_names: tuple[str, ...] = ()
            candidate = plan_result.get("candidate")
            if isinstance(candidate, dict):
                process_names = tuple(candidate.get("process_names") or ())
            self.state.note_action(
                action="PLAN",
                kind="app",
                display_name=display_name,
                verified=bool(verified),
                conversation_id=intent.conversation_id,
                turn_id=intent.turn_id or None,
                process_names=process_names,
                title_tokens=(display_name.casefold(),),
                hwnd=int(plan_result.get("hwnd") or 0) or None,
            )
            return
        path = str(plan_result.get("file") or "") or None
        filename = intent.arguments.get("filename") or intent.target
        self.state.note_action(
            action="PLAN", kind="file", display_name=filename,
            verified=bool(verified), conversation_id=intent.conversation_id,
            turn_id=intent.turn_id or None, path=path,
            title_tokens=(filename.casefold(),),
        )
        if verified and path:
            value, _ = self.state.get("last_target_file")
            self.state.update("last_created_artifact", value, source="filesystem",
                              ttl_seconds=1800, stale_after_seconds=7200)
            if self.artifacts is not None:
                self.artifacts.register(
                    path, kind="file",
                    conversation_id=intent.conversation_id,
                    source_turn_id=intent.turn_id or None,
                    exists_state="verified",
                    source_type="operator_created",
                )

    def observe_tool_result(self, tool_name: str, payload: dict[str, Any],
                            result, turn_id: str | None = None) -> None:
        if self.artifacts is not None:
            self.artifacts.observe_tool_result(
                tool_name, payload, result, turn_id,
            )

    def observe_assistant_response(
        self, response: str, *, conversation_id: str,
        turn_id: str | None, grounded: bool = False,
    ) -> None:
        if self.artifacts is not None:
            self.artifacts.observe_assistant_response(
                response, conversation_id=conversation_id,
                turn_id=turn_id, grounded=grounded,
            )

    def _learn(self, intent, effect, started: float, conversation_id: str,
               channel: str) -> None:
        from app.computer.usage import UsageEvent

        verified = bool(effect.verified) if effect is not None else False
        duration_ms = (time.perf_counter() - started) * 1000
        capabilities = ["ApplicationControl"]
        if intent.action in {"OPEN_FOLDER"}:
            capabilities = ["Filesystem"]
        elif intent.action in {"OPEN_FILE"}:
            capabilities = ["Filesystem", "ApplicationControl"]
        elif intent.action.endswith(("_APP",)) and intent.action != "OPEN_APP":
            capabilities = ["WindowControl"]
        event = UsageEvent(
            context_signature=self.usage.context_signature(
                self._foreground_process(), channel),
            intent=intent.action,
            target=intent.target[:80],
            action_sequence=[intent.action],
            capabilities_used=capabilities,
            verified_result=verified,
            duration_ms=round(duration_ms, 1),
            confidence=intent.confidence,
        )
        self.usage.record(event)
        if not verified:
            return
        operation = getattr(self.desktop, "last_operation_result", None) or {}
        alias = (intent.arguments.get("learned_alias") or intent.raw_target or "").strip()
        canonical = self._canonical_target(intent, operation)
        kind = {
            "OPEN_FOLDER": "folder", "OPEN_FILE": "file",
        }.get(intent.action, "app")
        if alias and canonical and alias.casefold() != canonical.casefold():
            self.usage.learn_alias_success(alias, canonical, kind=kind)
        # Alias learning de apps delega ao Universal Registry (fonte única);
        # aqui ficam padrões de sequência e preferências operacionais.
        sequence_context = self.usage.context_signature(
            f"conversation:{conversation_id or 'default'}", channel)
        steps = self.usage.track_sequence_step(sequence_context,
                                               f"{intent.action}:{intent.target}")
        if steps:
            from app.events import EventType

            candidate = self.usage.record_workflow(event.context_signature, steps,
                                                   success=True)
            if candidate is not None:
                existing = next((s for s in self.skills.skills.values()
                                 if s.source_workflow_id == candidate.workflow_id), None)
                if existing is None and candidate.confidence >= 0.5:
                    alias_hint = " ".join(intent.target.split()[:3])
                    created = self.skills.from_workflow_candidate(candidate,
                                                                  alias_hint=alias_hint)
                    del created
                if self.skills.event_bus is not None:
                    try:
                        task = asyncio.create_task(self.skills.event_bus.publish(
                            EventType.USAGE_PATTERN_DETECTED,
                            workflow_id=candidate.workflow_id,
                            confidence=candidate.confidence),
                            name="nyra-usage-pattern-event")
                        task.add_done_callback(
                            lambda done: None if done.cancelled() else done.exception())
                    except Exception:  # noqa: BLE001
                        pass

        if intent.action == "OPEN_FOLDER":
            key = f"pasta:{intent.target.casefold()}"
            self.usage.learn_preference(key, intent.target)

    def _record_skill_usage(self, skill, report) -> None:
        from app.computer.usage import UsageEvent

        real = UsageEvent(
            context_signature=self.usage.context_signature(self._foreground_process()),
            intent="SKILL_RUN", target=skill.name,
            action_sequence=[f"{step['capability']}:{step['target']}"
                             for step in report.steps],
            capabilities_used=["SkillMemory"],
            verified_result=report.ok,
        )
        self.usage.record(real)

    def _foreground_process(self) -> str | None:
        value, fresh = self.state.get("foreground_app")
        return value if value else None

    @staticmethod
    def _correction_target(text: str) -> str | None:
        match = re.match(
            r"^\s*n[aã]o\s*[,;:]?\s*(?:eu\s+)?(?:quis|queria)\s+dizer\s+"
            r"(?:o|a)?\s*(?P<target>.+?)\s*[.!]?\s*$",
            text or "", re.IGNORECASE)
        if match is None:
            return None
        return match.group("target").strip(" \"'‘’“”")[:80] or None

    @staticmethod
    def _canonical_target(intent, operation: dict[str, Any]) -> str:
        detail = operation.get("detail") if isinstance(operation.get("detail"), dict) else {}
        candidate = operation.get("candidate") \
            if isinstance(operation.get("candidate"), dict) else detail.get("candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        return str(candidate.get("display_name") or operation.get("app")
                   or intent.target).strip() or intent.target

    def _remember_resolution(self, intent, conversation_id: str,
                             operation: dict[str, Any]) -> None:
        alias = (intent.arguments.get("learned_alias") or intent.raw_target
                 or intent.target).strip()
        kind = {"OPEN_FOLDER": "folder", "OPEN_FILE": "file"}.get(
            intent.action, "app")
        if alias:
            self._last_resolutions[conversation_id or "default"] = {
                "alias": alias,
                "canonical": self._canonical_target(intent, operation),
                "kind": kind,
            }
        if len(self._last_resolutions) > 128:
            self._last_resolutions.pop(next(iter(self._last_resolutions)), None)

    async def _publish(self, event_name: str, **payload: Any) -> None:
        if self.event_bus is None:
            return
        try:
            from app.events import EventType

            payload.setdefault("source", "computer_autonomy")
            await self.event_bus.publish(getattr(EventType, event_name), **payload)
        except Exception:  # noqa: BLE001 — observabilidade nunca quebra ação
            logger.warning("computer_pipeline_event_failed event=%s", event_name)

    async def _record_failure(self, name: str, *, turn_id: str | None,
                              **payload: Any) -> None:
        """Incrementa e publica telemetria §103 sem conteúdo livre do usuário."""
        if name not in self.failure_metrics:
            return
        self.failure_metrics[name] += 1
        await self._publish(
            f"COMPUTER_{name.upper()}",
            turn_id=turn_id,
            count=self.failure_metrics[name],
            **payload,
        )

    def _finish(self, result: HandleResult, intent, started: float,
                conversation_id: str, turn_id: str | None,
                phase: dict[str, float] | None = None) -> None:
        result.metrics = {
            "total_operator_ms": round((time.perf_counter() - started) * 1000, 1),
            "intent_parse_ms": self.intents.metrics.get("intent_parse_ms", 0.0),
            "perception_ms": getattr(self.perception, "metrics", {}).get("perception_ms", 0.0),
            **(phase or {}),
        }
