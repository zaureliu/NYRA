from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
import re
import time

from app.avatar import AvatarController
from app.character.context import is_simple_conversation, is_standalone_greeting
from app.character.state import EmotionalState as CharacterEmotionalState
from app.character.response_style import apply_response_style
from app.core.turn import (
    PipelineFailure,
    TurnContext,
    TurnError,
    TurnRegistry,
    TurnStatus,
    reset_current_turn_id,
    set_current_turn_id,
)
from app.events import EventType
from app.memory.models import MemoryCategory, MemoryCreate
from app.orchestrator import ChatOrchestrator, ChatResult
from app.perception import ContextSelector, PCAwareness
from app.realtime.models import RealtimeStatus
from app.realtime.sentence_assembler import SentenceAssembler
from app.realtime.settings import V4SettingsManager
from app.realtime.telemetry import RealtimeTelemetry
from app.speech.queue import SpeechPriority
from app.speech.profile import load_voice_profile
from app.speech.voice_processor import VoiceProcessor
from app.operator.monitoring import (
    enforce_monitor_promise,
    is_monitor_cancel_request,
)


logger = logging.getLogger("kazumi.realtime")


class RealtimeOrchestrator(ChatOrchestrator):
    """Progressive LLM -> sentence -> TTS pipeline with ordered cancellation.

    Every invocation owns one TurnContext: stream accumulators, tool results and
    observations are turn-scoped and never reused across turns. A TTS failure
    degrades audio only (TEXT_COMPLETE/AUDIO_DEGRADED) instead of failing the
    completed text response.
    """

    def __init__(self, *args, settings_manager: V4SettingsManager, telemetry: RealtimeTelemetry,
                 perception: PCAwareness, avatar: AvatarController, voice_processor: VoiceProcessor,
                 turn_registry: TurnRegistry | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.settings_manager = settings_manager
        self.telemetry = telemetry
        self.perception = perception
        self.avatar = avatar
        self.voice_processor = voice_processor
        self.context_selector = ContextSelector()
        self.status = RealtimeStatus.IDLE
        self._active_response_id: str | None = None
        self._active_task: asyncio.Task | None = None
        self._cancel_event: asyncio.Event | None = None
        self._speech_cancelled_ids: set[str] = set()
        self.skills = None
        self.remote_shell = None
        self.agent = None
        self.monitor_jobs = None
        self.open_loops = None
        self.world_state = None
        self.emotional_presence = None
        self.usb_devices = None
        self.turns = turn_registry or TurnRegistry()
        # Universal Operator (kazumi-full): injetado pelo main quando disponível.
        self.desktop = None

    async def converse(
        self,
        text: str,
        synthesize: bool = True,
        *,
        speech_end: float | None = None,
        response_id: str | None = None,
        turn: TurnContext | None = None,
    ) -> ChatResult:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("Mensagem vazia")
        if turn is None:
            turn = TurnContext(clean_text)
            if response_id:
                turn.turn_id = f"turn_{response_id}" if not response_id.startswith("turn_") else response_id
        turn.user_input = clean_text
        turn_id = turn.turn_id
        response_key = turn.response_id
        self.turns.start(turn)
        turn_token = set_current_turn_id(turn_id)
        current_task = asyncio.current_task()
        try:
            # Tudo a partir daqui precisa estar dentro do try: o turno antigo
            # pode ser cancelado pelo novo input a qualquer await — inclusive
            # durante telemetry/publish antes de _run_turn (§156).
            if self._active_task and not self._active_task.done() and self._active_task is not current_task:
                await self.interrupt("new_user_turn")
            # Invariante 4 (Apêndice PRO C): novo turno invalida a fila antiga.
            dropped_stale = await self.speech_queue.purge_except(response_key)
            if dropped_stale:
                logger.info("stale_speech_purged", extra={"turn_id": turn_id, "dropped": dropped_stale})
            cancel_event = asyncio.Event()
            self._active_response_id, self._active_task, self._cancel_event = response_key, current_task, cancel_event
            self.telemetry.start(response_key, speech_end=speech_end or time.perf_counter())
            self.telemetry.mark(response_key, "t_request_received")
            self.telemetry.mark(response_key, "t_stt_final")
            await self.event_bus.publish(EventType.USER_TEXT_RECEIVED, response_id=response_key, turn_id=turn_id)
            logger.info(
                "TURN START",
                extra={"turn_id": turn_id, "conversation_id": turn.conversation_id, "user_length": len(clean_text)},
            )
            return await self._run_turn(turn, clean_text, synthesize, cancel_event)
        except asyncio.CancelledError:
            await self._cancelled(response_key, "task_cancelled")
            self.turns.finish(turn_id, TurnStatus.CANCELLED)
            if self._active_task is not None and self._active_task is not current_task:
                # §156: input concorrente substitui o turno anterior por policy.
                # O pedido antigo responde com erro estruturado (HTTP 409 no
                # /api/chat) em vez de vazar CancelledError como 500.
                raise PipelineFailure(TurnError(
                    stage="pipeline", error_code="TURN_SUPERSEDED",
                    exception_type="CancelledError",
                    message="Turno substituído por novo input do usuário.",
                    recoverable=True, turn_id=turn_id,
                )) from None
            raise
        except Exception as exc:
            error = TurnError(
                stage=self._failure_stage(exc),
                error_code="PIPELINE_FAILURE",
                exception_type=type(exc).__name__,
                message=str(exc)[:300],
                recoverable=True,
                turn_id=turn_id,
            )
            logger.error(
                "turn_failed",
                extra={
                    "turn_id": turn_id,
                    "stage": error.stage,
                    "error_code": error.error_code,
                    "exception_type": error.exception_type,
                    "detail": error.message,
                },
                exc_info=True,
            )
            self.turns.finish(turn_id, TurnStatus.FAILED, error=error)
            raise PipelineFailure(error) from exc
        finally:
            reset_current_turn_id(turn_token)
            if self._active_response_id == response_key:
                self._active_response_id = None
                self._active_task = None
                self._cancel_event = None
            self._speech_cancelled_ids.discard(response_key)

    @staticmethod
    def _failure_stage(exc: BaseException) -> str:
        message = str(exc).casefold()
        if "ollama" in message or "stream" in message:
            return "llm"
        if "tts" in message or "speech" in message or "audio" in message:
            return "tts"
        return "pipeline"

    async def _run_turn(self, turn: TurnContext, clean_text: str, synthesize: bool,
                        cancel_event: asyncio.Event) -> ChatResult:
        turn_id = turn.turn_id
        response_id = turn.response_id
        state = await self.state_machine.infer_and_transition(clean_text)
        memory_write_started = time.perf_counter()
        await self.memory.add(MemoryCreate(category=MemoryCategory.SHORT_TERM, role="user", content=clean_text, importance=5))
        self.telemetry.measure(response_id, "memory_write_ms", (time.perf_counter()-memory_write_started)*1000)
        tools_started = time.perf_counter()
        runtime_parts: list[str] = []
        voice_session = getattr(self, "voice_session", None)
        is_voice_session = voice_session is not None and turn.conversation_id == voice_session.conversation_id
        if is_voice_session:
            runtime_parts.append(voice_session.context())
        direct_response = "Oi. O que precisa?" if is_standalone_greeting(clean_text) and not is_voice_session else None
        resume_agent_run_id: str | None = None
        route_to_agent = bool(self.tools is not None and self.tools.should_route_to_agent(clean_text))
        hardware_engine = getattr(self, 'hardware_engine', None)
        if is_voice_session:
            from app.natural_conversation.tool_bridge import cancellation_requested, cancel_session_task
            if cancellation_requested(clean_text):
                direct_response = await cancel_session_task(voice_session, hardware_engine)
                route_to_agent = False
        if hardware_engine is not None and self.tools is not None and direct_response is None:
            from app.web_research.conversation import WebConversationBridge
            if not hasattr(self, '_web_conversation'):
                self._web_conversation = WebConversationBridge(hardware_engine.research, self.tools)
            direct_response = await self._web_conversation.reply(clean_text, turn)
            if direct_response is not None:
                route_to_agent = False
        if hardware_engine is not None and direct_response is None:
            direct_response = await hardware_engine.handle(clean_text)
            if direct_response is not None:
                route_to_agent = False
        # Hardware is an observation request even when phrased as a user claim.
        # Resolve before conversational/goal shortcuts and before any token/TTS.
        from app.usb.hardware import hardware_request, presence_reply, UNKNOWN_RESPONSE
        hardware_intent = hardware_request(clean_text)
        if hardware_intent is not None and direct_response is None:
            direct_response = UNKNOWN_RESPONSE
            route_to_agent = False
            if self.tools is not None:
                try:
                    result = await self.tools.execute("hardware_discover", hardware_intent.model_dump())
                    direct_response = presence_reply(result.data)
                except Exception:
                    logger.warning("hardware_discovery_unavailable", extra={"turn_id": turn_id})
            elif self.usb_devices is not None:
                direct_response = await self.usb_devices.handle_chat(clean_text) or UNKNOWN_RESPONSE
        if self.open_loops is not None and direct_response is None:
            try:
                loop_project = None
                if self.world_state is not None:
                    project_record = self.world_state.get_snapshot().get("current_project")
                    project_value = project_record.get("value") if isinstance(project_record, dict) else None
                    loop_project = project_value if isinstance(project_value, str) else None
                await self.open_loops.observe_user_intention(
                    clean_text, source_turn=turn_id, project=loop_project,
                )
                loop_response = await self.open_loops.chat_response(
                    clean_text, source_turn=turn_id, project=loop_project,
                )
                if loop_response is not None:
                    direct_response = loop_response
                    route_to_agent = False
            except (PermissionError, ValueError):
                # Invalid/secret-bearing candidates are not persisted and do
                # not prevent the normal conversational pipeline.
                pass
        if (
            self.monitor_jobs is not None
            and direct_response is None
            and is_monitor_cancel_request(clean_text)
        ):
            cancelled = await self.monitor_jobs.cancel_from_text(clean_text)
            direct_response = str(cancelled.get("message") or "Não consegui cancelar o monitoramento.")
            route_to_agent = False
        # UNIVERSAL OPERATOR fast path (kazumi-full §25/§41 / kazumi-7c §75):
        # pipeline unificado das 7 camadas quando presente; sem ele, o bloco
        # legacy abaixo mantém o comportamento anterior (compatibilidade).
        computer = getattr(self, "computer", None)
        universal_handled = False
        if self.usb_devices is not None and direct_response is None:
            try:
                usb_response = await self.usb_devices.handle_chat(clean_text)
                if usb_response is not None:
                    direct_response = usb_response
                    route_to_agent = False
                    universal_handled = True
            except Exception as error:  # noqa: BLE001 - USB opcional não derruba chat
                logger.warning(
                    "usb_chat_intent_failed",
                    extra={"turn_id": turn_id, "exception_type": type(error).__name__},
                )
        if computer is not None and direct_response is None:
            try:
                handle_result = await computer.handle_user_request(
                    clean_text,
                    conversation_id=turn.conversation_id,
                    turn_id=turn_id,
                )
                if handle_result.handled:
                    direct_response = handle_result.reply
                    route_to_agent = False  # ONE ACTION OWNER (§11/§34)
                    universal_handled = True
                    for metric_key, metric_value in handle_result.metrics.items():
                        self.telemetry.measure(response_id, metric_key, metric_value)
            except Exception as error:  # noqa: BLE001 — pipeline nunca derruba turno
                logger.warning(
                    "computer_pipeline_failed",
                    extra={"turn_id": turn_id, "exception_type": type(error).__name__},
                )
        elif self.desktop is not None and direct_response is None:
            from app.desktop.intents import parse_notepad_multistep, parse_universal_intent

            mstep = parse_notepad_multistep(clean_text)
            if mstep is not None:
                from app.desktop.multistep import notepad_write_and_save

                result = await notepad_write_and_save(
                    self.desktop, mstep["text"], mstep["filename"]
                )
                direct_response = result["message"]
                route_to_agent = False
                universal_handled = True
            else:
                uintent = parse_universal_intent(clean_text)
                if uintent is not None:
                    handled, reply = await self.desktop.handle_universal(uintent, turn_id=turn_id)
                    if handled:
                        direct_response = reply
                        route_to_agent = False  # ONE ACTION OWNER (§11)
                        universal_handled = True
        # FAST PATH: conversa trivial não atravessa percepção, sentinel, network
        # nem seleção de contexto operacional — contexto casual mínimo + streaming.
        fast_conversation = (
            direct_response is None
            and not route_to_agent
            and is_simple_conversation(clean_text)
        )
        if universal_handled:
            tool_context_ms = (time.perf_counter() - tools_started) * 1000
            self.telemetry.measure(response_id, "tools_ms", tool_context_ms)
        elif not fast_conversation:
            if self.agent is not None and re.fullmatch(r"(?i)\s*(?:kazumi[, ]+)?(?:para|pare|cancela|cancelar|interrompe|interromper)\s*[.!]?\s*", clean_text):
                cancelled = await self.agent.cancel_active("operator_voice_or_chat")
                direct_response = "Interrompi o Agent Run ativo." if cancelled else "Não há Agent Run ativo para interromper."
            if self.shell is not None and turn.approval_capable:
                approval = await self.shell.resolve_user_approval(clean_text)
                if approval is not None:
                    route_to_agent = True
                    if approval.status == "GRANTED":
                        resume_agent_run_id = approval.agent_run_id
                    elif self.agent is not None:
                        await self.agent.approval_denied(approval.agent_run_id)
                        direct_response = "A operação pendente foi cancelada e não será executada."
                    tool_name = "remote_shell" if approval.shell == "ssh" else "system_shell"
                    runtime_parts.append(
                        "SHELL_APPROVAL_DECISION="
                        + json.dumps(approval.public_dict(), ensure_ascii=False)
                        + (f"\nContinue o mesmo agent_run e reenvie exatamente o mesmo {tool_name} com este approval_id." if approval.status == "GRANTED" else "\nA operação foi negada; não a execute.")
                    )
                if route_to_agent:
                    shell_status = self.shell.status()
                    runtime_parts.append(
                        "SYSTEM_SHELL_STATUS="
                        + json.dumps(
                            {
                                "enabled": shell_status["enabled"],
                                "default_shell": shell_status["default_shell"],
                                "default_working_directory": shell_status["default_working_directory"],
                                "max_calls_per_turn": shell_status["max_calls_per_turn"],
                            },
                            ensure_ascii=False,
                        )
                    )
            if self.remote_shell is not None and route_to_agent:
                remote_status = self.remote_shell.status()
                runtime_parts.append(
                    "TRUSTED_REMOTE_SHELL="
                    + json.dumps(
                        {
                            "enabled": remote_status["enabled"],
                            "registered_hosts": remote_status["hosts"],
                            "rule": "remote_shell accepts only logical id/alias; never pass an IP, username, port or credential",
                        },
                        ensure_ascii=False,
                    )
                )
            if self.agent is not None and route_to_agent:
                runtime_parts.append("AGENT_POLICY=" + json.dumps(self.agent.status(), ensure_ascii=False))
            if self.sentinel_watch is not None:
                sentinel_response = await self.sentinel_watch.explicit_command(clean_text)
                if sentinel_response is not None:
                    direct_response = sentinel_response
            if self.network_watch is not None and re.search(r"\b(rede|internet|conex[aã]o|lat[eê]ncia|jitter|pacotes?|gateway|dns)\b", clean_text, re.IGNORECASE):
                runtime_parts.append("NETWORK_WATCH=" + json.dumps(self.network_watch.status(), ensure_ascii=False))
                if self.skills is not None and re.search(r"\b(verifica|verificar|confere|checa|como est[aá])\b", clean_text, re.IGNORECASE):
                    try:
                        result = await self.skills.execute("network_status", {})
                        runtime_parts.append("SKILL_RESULT[network_status]=" + json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
                    except (KeyError, RuntimeError, ValueError):
                        pass
            # kazumi-full §13: listar arquivos ≠ abrir pasta. Diretriz determinística
            # para o domínio LLM (filesystem_list_files), sem sequestrar o pipeline.
            elif re.search(r"\b(?:mostra|mostre|liste|lista|quais)\b.{0,60}\barquivos?\b",
                           clean_text, re.IGNORECASE):
                runtime_parts.append(
                    "FILESYSTEM_INTENT=LIST_ONLY: o operador quer LISTAR arquivos; "
                    "use filesystem_list_files. NÃO abra pasta, app ou janela para isso."
                )
            if self.sentinel_watch is not None and re.search(r"\bsentinel\b", clean_text, re.IGNORECASE):
                runtime_parts.append("UTAMO_SENTINEL=" + json.dumps(await self.sentinel_watch.summary(24), ensure_ascii=False))
            selected_perception = self.context_selector.select(clean_text, self.perception.snapshot)
            if selected_perception:
                runtime_parts.append("LOCAL_PC_AWARENESS=" + selected_perception)
        tool_context_ms = (time.perf_counter()-tools_started)*1000
        self.telemetry.measure(response_id, "tools_ms", tool_context_ms)
        context_started = time.perf_counter(); context_timings: dict[str,float] = {}
        messages = await self.context.build(
            clean_text,
            state,
            "\n".join(runtime_parts),
            context_timings,
            tool_context=route_to_agent,
        )
        self.telemetry.measure(response_id, "context_build_ms", (time.perf_counter()-context_started)*1000)
        for key,value in context_timings.items(): self.telemetry.measure(response_id,key,value)
        await self._set_status(RealtimeStatus.THINKING, response_id, turn_id)
        await self.avatar.mode("thinking")
        await self.event_bus.publish(EventType.LLM_PROCESSING, state=state.value, response_id=response_id, turn_id=turn_id)
        await self.event_bus.publish(EventType.LLM_STREAM_STARTED, response_id=response_id, provider=self.llm.name, turn_id=turn_id)
        self.telemetry.mark(response_id, "t_llm_stream_started")

        config = self.settings_manager.value.realtime
        assembler = SentenceAssembler(config.minimum_chunk_characters, config.minimum_chunk_words, config.chunk_timeout_ms)
        speech_input: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue(maxsize=24)
        audio_urls: list[str] = []
        speech_degraded = asyncio.Event()
        speech_task = asyncio.create_task(
            self._speech_worker(
                response_id,
                turn_id,
                clean_text,
                state.value,
                speech_input,
                audio_urls,
                cancel_event,
                speech_degraded,
                {"technical": bool(route_to_agent or not fast_conversation)},
            ),
            name=f"kazumi-speech-stream-{response_id[:8]}",
        ) if synthesize and await self.tts.health() else None
        sentence_index = 0
        first_token = True
        self.telemetry.mark(response_id, "t_ollama_request")
        if direct_response:
            stream = self._direct_stream(direct_response)
        elif route_to_agent and self.tools is not None:
            await self._set_status(RealtimeStatus.TOOL_EXECUTION, response_id, turn_id)
            agent_started = time.perf_counter()
            if self.agent is not None:
                agent_response = await self.agent.run(
                    messages, clean_text,
                    resume_run_id=resume_agent_run_id,
                    external_cancel_event=cancel_event,
                    turn_id=turn_id,
                    conversation_id=turn.conversation_id,
                )
                turn.agent_run_id = self.agent.last_run_for_turn(turn_id) or turn.agent_run_id
            else:
                from app.tools.agent import ToolAgentLoop
                agent_response = await ToolAgentLoop(
                    self.llm, self.tools,
                    self.shell.settings.shell_max_calls_per_turn if self.shell is not None else 10,
                ).run(messages, turn_id=turn_id)
            agent_response = enforce_monitor_promise(
                agent_response,
                job_created=bool(
                    self.monitor_jobs is not None
                    and self.monitor_jobs.has_job_for_turn(turn_id)
                ),
            )
            self.telemetry.measure(
                response_id,
                "tools_ms",
                tool_context_ms + (time.perf_counter() - agent_started) * 1000,
            )
            stream = self._direct_stream(agent_response)
        else:
            stream = self.llm.stream(messages) if config.streaming_responses else self._direct_stream(await self.llm.chat(messages))
        try:
            async for token in stream:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                if first_token:
                    self.telemetry.mark(response_id, "t_llm_first_token")
                    first_token = False
                turn.append_content(token)
                await self.event_bus.publish(EventType.LLM_TOKEN_RECEIVED, response_id=response_id, turn_id=turn_id, delta=token)
                sentences = assembler.feed(token) if config.sentence_streaming else []
                sentences.extend(assembler.flush_due())
                for sentence in sentences:
                    sentence_index = await self._queue_sentence(response_id, turn_id, sentence_index, sentence, speech_input, speech_task)
            for sentence in assembler.finish():
                sentence_index = await self._queue_sentence(response_id, turn_id, sentence_index, sentence, speech_input, speech_task)
            self.telemetry.mark(response_id, "t_ollama_complete")
            for key,value in getattr(self.llm,"last_runtime_metrics",{}).items(): self.telemetry.measure(response_id,key,value)
            if speech_task and not speech_task.done():
                await speech_input.put(None)
                try:
                    await speech_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # TTS runs behind its own boundary: synthesis failures must not
                    # invalidate the already-completed text response.
                    speech_degraded.set()
                    logger.warning("speech_worker_failed_after_text", extra={"turn_id": turn_id, "exception_type": type(exc).__name__})
        except BaseException:
            if speech_task and not speech_task.done():
                speech_task.cancel()
            await self.speech_queue.cancel(response_id)
            raise

        response = apply_response_style("".join(turn.content_buffer))
        response = enforce_monitor_promise(
            response,
            job_created=bool(
                self.monitor_jobs is not None
                and self.monitor_jobs.has_job_for_turn(turn_id)
            ),
        )
        if not response:
            raise RuntimeError("LLM returned an empty response")
        if computer is not None and not universal_handled:
            computer.observe_assistant_response(
                response,
                conversation_id=turn.conversation_id,
                turn_id=turn_id,
                grounded=bool(route_to_agent),
            )
        if not is_voice_session:
            await self.memory.add(MemoryCreate(category=MemoryCategory.SHORT_TERM, role="assistant", content=response, importance=5))
        await self.memory.retain()
        prepared = self.prosody.prepare(response, provider=self.tts.name)
        if audio_urls:
            emotion_plan = self.emotion_planner.dominant(turn_id)
        else:
            planning_started = time.perf_counter()
            emotion_plan = self.emotion_planner.plan(
                clean_text,
                response,
                context={"technical": bool(route_to_agent or not fast_conversation)},
                turn_id=turn_id,
            )
            self.telemetry.measure(response_id, "emotion_planning_ms", (time.perf_counter() - planning_started) * 1000)
            emotion_plan = self.emotion_planner.dominant(turn_id, emotion_plan)
        state = await self.state_machine.transition(
            state.__class__(emotion_plan.emotion.value),
            intensity=emotion_plan.intensity,
            confidence=emotion_plan.confidence,
            reason=emotion_plan.reason,
        )
        persona_runtime = getattr(self.state_machine, "persona_runtime", None)
        canonical_emotion = await persona_runtime.current_emotion() if persona_runtime is not None else None
        voice_build = self.emotional_presence.build_voice_style(
            emotion=state.value,
            intensity=canonical_emotion.intensity if canonical_emotion is not None else emotion_plan.intensity,
            context={"source": "chat", "turn_id": turn_id},
        ) if self.emotional_presence is not None else None
        provider_capabilities = self.tts.capabilities() if callable(getattr(self.tts, "capabilities", None)) else None
        voice_interface = voice_build.presentation if voice_build else (
            persona_runtime.voice_interface(
                provider_supports_emotion=bool(getattr(provider_capabilities, "supports_emotion", False)),
            ) if persona_runtime is not None else None
        )
        await self.event_bus.publish(
            EventType.KAZUMI_RESPONSE, response_id=response_id, turn_id=turn_id, text=response,
            display_text=prepared.display_text, speech_text=prepared.speech_text, state=state.value,
            emotion_intensity=emotion_plan.intensity,
            emotion_engine_supported=bool(getattr(provider_capabilities, "supports_emotion", False)),
            dialogue_policy=(persona_runtime.dialogue_policy.mode.value if persona_runtime else None),
            voice_emotion=voice_interface.model_dump(mode="json") if voice_interface else None,
            voice_style=voice_build.presentation.model_dump(mode="json") if voice_build else None,
        )
        self.telemetry.mark(response_id, "t_response_complete")
        metrics = self.telemetry.finish(response_id)
        logger.info(
            "PERF request=%s model=%s stt=%sms memory=%sms context=%sms tools=%sms prompt_chars=%s ollama_connect=%sms ollama_first_token=%sms ollama_generation=%sms ollama_total=%sms tts_first_audio=%sms total=%sms",
            response_id,
            getattr(self.llm, "active_model", None) or getattr(self.llm, "model", None) or self.llm.name,
            metrics.get("stt_total_ms"), metrics.get("memory_lookup_ms"), metrics.get("context_build_ms"),
            metrics.get("tools_ms"), metrics.get("prompt_characters"), metrics.get("ollama_connect_ms"),
            metrics.get("ollama_first_token_ms"), metrics.get("ollama_generation_ms"), metrics.get("ollama_total_ms"),
            metrics.get("tts_first_audio_ms"), metrics.get("request_total_ms"),
        )
        if speech_degraded.is_set():
            final_status = TurnStatus.AUDIO_DEGRADED
            await self.event_bus.publish(EventType.TTS_FAILED, response_id=response_id, turn_id=turn_id, reason="chunk_synthesis_error")
        else:
            final_status = TurnStatus.COMPLETE if audio_urls else TurnStatus.TEXT_COMPLETE
        logger.info(
            "TEXT COMPLETE",
            extra={"turn_id": turn_id, "status": final_status.value, "audio_chunks": len(audio_urls)},
        )
        logger.info("TURN END", extra={"turn_id": turn_id, "status": final_status.value})
        self.turns.finish(turn_id, final_status, final_response=response)
        await self._set_status(RealtimeStatus.IDLE, response_id, turn_id)
        await self.avatar.mode("idle")
        return ChatResult(
            response_id=response_id, turn_id=turn_id, pipeline_status=final_status.value, response=response,
            display_text=prepared.display_text, speech_text=prepared.speech_text, state=state.value,
            emotion_intensity=emotion_plan.intensity,
            audio_url=None, audio_urls=audio_urls, tts_provider=self.tts.name if audio_urls else None,
            timing={key: value for key, value in metrics.items() if isinstance(value, (int, float)) and value is not None},
        )

    async def _queue_sentence(self, response_id: str, turn_id: str, index: int, sentence: str,
                              queue: asyncio.Queue, speech_task: asyncio.Task | None) -> int:
        styled = apply_response_style(sentence)
        if not styled:
            return index
        if index == 0:
            self.telemetry.mark(response_id, "t_first_sentence")
        await self.event_bus.publish(EventType.SENTENCE_READY, response_id=response_id, turn_id=turn_id, index=index, characters=len(styled))
        if speech_task and not speech_task.done() and response_id not in self._speech_cancelled_ids:
            pending = asyncio.create_task(queue.put((index, styled)))
            await asyncio.wait((pending, speech_task), return_when=asyncio.FIRST_COMPLETED)
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        return index + 1

    async def _speech_worker(self, response_id: str, turn_id: str, user_text: str, state: str, queue: asyncio.Queue,
                             audio_urls: list[str], cancel_event: asyncio.Event,
                             degraded: asyncio.Event, emotion_context: dict[str, object]) -> None:
        first = True
        last_state = state
        while True:
            item = await queue.get()
            if item is None:
                break
            index, sentence = item
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if response_id in self._speech_cancelled_ids:
                return
            planning_started = time.perf_counter()
            plan = self.emotion_planner.plan(
                user_text,
                sentence,
                context=emotion_context,
                turn_id=turn_id,
                sentence_index=index,
            )
            self.telemetry.measure(response_id, "emotion_planning_ms", (time.perf_counter() - planning_started) * 1000)
            persona_runtime = getattr(self.state_machine, "persona_runtime", None)
            if persona_runtime is not None:
                await self.state_machine.transition(
                    CharacterEmotionalState(plan.emotion.value),
                    intensity=plan.intensity,
                    confidence=plan.confidence,
                    reason=plan.reason,
                )
                canonical_emotion = await persona_runtime.current_emotion()
                last_state = canonical_emotion.primary.value
                applied_intensity = canonical_emotion.intensity
            else:
                last_state = plan.emotion.value
                applied_intensity = plan.intensity
            capabilities = self.tts.capabilities() if callable(getattr(self.tts, "capabilities", None)) else None
            emotion_supported = bool(getattr(capabilities, "supports_emotion", False))
            acoustic_state = last_state if emotion_supported else "neutral"
            voice_build = self.emotional_presence.build_voice_style(
                emotion=last_state,
                intensity=applied_intensity,
                context={"source": "streaming_chat", "turn_id": turn_id, "sentence_index": index},
            ) if self.emotional_presence is not None else None
            voice_interface = voice_build.presentation if voice_build else (
                persona_runtime.voice_interface(
                    provider_supports_emotion=emotion_supported,
                    emotion=last_state,
                    intensity=applied_intensity,
                )
                if persona_runtime is not None else None
            )
            if voice_build is not None:
                acoustic_state = voice_build.presentation.acoustic_emotion
                options = voice_build.options
            elif voice_interface is not None:
                last_state = voice_interface.emotion.value
                acoustic_state = voice_interface.acoustic_emotion
                if last_state != plan.emotion.value:
                    from app.speech.emotion import EmotionPlan

                    plan = EmotionPlan.validated(
                        last_state, voice_interface.intensity,
                        confidence=plan.confidence, reason="persona_runtime_voice_fallback",
                        turn_id=turn_id, sentence_index=index,
                    )
                _profile, defaults = load_voice_profile()
                options = defaults.with_emotion(plan)
            else:
                _profile, defaults = load_voice_profile()
                options = defaults.with_emotion(plan)
            prepared = self.prosody.prepare(sentence, provider=self.tts.name)
            from app.natural_conversation.speech_planner import plan_speech
            speech_plan = plan_speech(prepared.speech_text, emotion=last_state,
                                      intensity=applied_intensity, capabilities=capabilities)
            if first:
                await self.event_bus.publish(
                    EventType.TTS_STARTED,
                    state=last_state,
                    emotion_intensity=applied_intensity,
                    emotion_engine_supported=emotion_supported,
                    response_id=response_id,
                    turn_id=turn_id,
                    streaming=True,
                    voice_emotion=voice_interface.model_dump(mode="json") if voice_interface else None,
                    voice_style=voice_build.presentation.model_dump(mode="json") if voice_build else None,
                )
                self.telemetry.mark(response_id, "t_tts_start")
            await self.event_bus.publish(EventType.TTS_CHUNK_STARTED, response_id=response_id, turn_id=turn_id, index=index, state=last_state, emotion_intensity=applied_intensity, speech_text=speech_plan.spoken_text)
            packet_index = 0
            packet_rate = 24000
            async def emit_audio(packet):
                nonlocal packet_index, packet_rate, first
                if response_id in self._speech_cancelled_ids:
                    raise asyncio.CancelledError
                if packet.timestamps and not packet.pcm and not packet.path:
                    # Provider alignment is optional, never an empty audio chunk.
                    return
                packet_rate = packet.sample_rate
                if packet.path is not None:
                    url = f"/api/audio/{packet.path.name}"
                    audio_urls.append(url)
                    await self.event_bus.publish(EventType.TTS_CHUNK_FINISHED, response_id=response_id,
                        turn_id=turn_id, index=index, audio_url=url, state=last_state,
                        display_text=sentence, speech_text=speech_plan.spoken_text)
                else:
                    await self.event_bus.publish(EventType.TTS_PCM_CHUNK, response_id=response_id,
                        turn_id=turn_id, index=index, packet_index=packet_index, sample_rate=packet.sample_rate,
                        pcm=base64.b64encode(packet.pcm).decode("ascii"), final=False)
                    packet_index += 1
                if first:
                    self.telemetry.mark(response_id, "t_first_audio")
                    await self._set_status(RealtimeStatus.SPEAKING, response_id, turn_id)
                    await self.avatar.mode("speaking")
                    first = False
            try:
                if capabilities and capabilities.supports_streaming:
                    await self.speech_queue.synthesize(
                        self.tts, speech_plan.spoken_text, acoustic_state, SpeechPriority.USER,
                        response_id=response_id, chunk_index=index, turn_id=turn_id,
                        options=options, on_audio=emit_audio,
                    )
                    if packet_index:
                        await self.event_bus.publish(EventType.TTS_PCM_CHUNK, response_id=response_id,
                            turn_id=turn_id, index=index, packet_index=packet_index, sample_rate=packet_rate,
                            pcm="", final=True)
                    continue
                output = await self.speech_queue.synthesize(
                    self.tts, speech_plan.spoken_text, acoustic_state, SpeechPriority.USER,
                    response_id=response_id, chunk_index=index, turn_id=turn_id,
                    options=options,
                )
            except asyncio.CancelledError:
                if response_id in self._speech_cancelled_ids:
                    return
                raise
            except Exception as exc:
                # Chunk-level isolation: one failed chunk degrades audio without
                # poisoning assistant text, other chunks or the next turn.
                degraded.set()
                logger.warning(
                    "tts_chunk_failed",
                    extra={"turn_id": turn_id, "index": index, "exception_type": type(exc).__name__},
                )
                await self.event_bus.publish(EventType.TTS_CHUNK_FAILED, response_id=response_id, turn_id=turn_id, index=index, exception_type=type(exc).__name__)
                continue
            audio_url = f"/api/audio/{Path(output).name}"
            audio_urls.append(audio_url)
            if first:
                self.telemetry.mark(response_id, "t_first_audio")
                await self._set_status(RealtimeStatus.SPEAKING, response_id, turn_id)
                await self.avatar.mode("speaking")
                first = False
            await self.event_bus.publish(
                EventType.TTS_CHUNK_FINISHED, response_id=response_id, turn_id=turn_id, index=index,
                audio_url=audio_url, state=last_state, emotion_intensity=applied_intensity, display_text=sentence,
                speech_text=speech_plan.spoken_text, speech_plan=speech_plan.metadata(),
            )
        if not first:
            await self.event_bus.publish(EventType.TTS_FINISHED, state=last_state, response_id=response_id, turn_id=turn_id, streaming=True)

    async def interrupt(self, reason: str = "user_barge_in") -> bool:
        response_id = self._active_response_id
        if not response_id:
            return False
        if self._cancel_event:
            self._cancel_event.set()
        await self.speech_queue.cancel(response_id)
        task = self._active_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        else:
            await self._cancelled(response_id, reason)
        return True

    async def cancel_speech(self, reason: str = "user_barge_in") -> bool:
        """Stop TTS generation/playback events without cancelling LLM/tools/Agent work."""
        session = getattr(self, "voice_session", None)
        response_id = (session.playing_response if session else None) or self._active_response_id
        if not response_id:
            return False
        self._speech_cancelled_ids.add(response_id)
        if len(self._speech_cancelled_ids) > 256:
            stale = next((key for key in self._speech_cancelled_ids if key not in {response_id, self._active_response_id}), None)
            if stale:
                self._speech_cancelled_ids.discard(stale)
        if session:
            session.interrupt(response_id)
        self.emotion_planner.cancel_turn(response_id if response_id.startswith("turn_") else f"turn_{response_id}")
        cancelled = await self.speech_queue.cancel(response_id)
        await self.event_bus.publish(EventType.USER_INTERRUPTED, response_id=response_id, reason=reason, speech_only=True)
        await self.event_bus.publish(EventType.SPEECH_CANCELLED, response_id=response_id, reason=reason)
        await self._set_status(RealtimeStatus.INTERRUPTED, response_id)
        await self.avatar.mode("listening")
        await self._set_status(RealtimeStatus.LISTENING, response_id)
        return cancelled > 0 or self.status == RealtimeStatus.LISTENING

    async def _cancelled(self, response_id: str, reason: str) -> None:
        self.emotion_planner.cancel_turn(f"turn_{response_id}")
        self.telemetry.record("USER_INTERRUPTED", response_id=response_id, reason=reason)
        await self._set_status(RealtimeStatus.INTERRUPTED, response_id)
        await self.event_bus.publish(EventType.USER_INTERRUPTED, response_id=response_id, reason=reason)
        await self.event_bus.publish(EventType.SPEECH_CANCELLED, response_id=response_id, reason=reason)
        await self.event_bus.publish(EventType.REALTIME_CANCELLED, response_id=response_id, reason=reason)
        await self.avatar.mode("listening")
        await self._set_status(RealtimeStatus.LISTENING, response_id)

    async def _set_status(self, status: RealtimeStatus, response_id: str, turn_id: str | None = None) -> None:
        self.status = status
        await self.event_bus.publish(EventType.REALTIME_STATUS_CHANGED, status=status.value, response_id=response_id, turn_id=turn_id)

    @staticmethod
    async def _direct_stream(value: str | None):
        if value:
            yield value

    def debug_status(self) -> dict:
        return {
            "status": self.status.value, "response_id": self._active_response_id,
            "sentence_queue": self.speech_queue.pending,
            "tts_counters": dict(getattr(self.speech_queue, "counters", {})),
            "turns": self.turns.snapshot(),
        }
