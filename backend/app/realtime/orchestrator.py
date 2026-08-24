from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import time

from app.avatar import AvatarController
from app.character.context import is_standalone_greeting
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
from app.speech.voice_processor import VoiceProcessor


logger = logging.getLogger("nyra.realtime")


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
        self.turns = turn_registry or TurnRegistry()

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
        direct_response = "Oi. O que precisa?" if is_standalone_greeting(clean_text) else None
        resume_agent_run_id: str | None = None
        route_to_agent = bool(self.tools is not None and self.tools.should_route_to_agent(clean_text))
        if self.agent is not None and re.fullmatch(r"(?i)\s*(?:nyra[, ]+)?(?:para|pare|cancela|cancelar|interrompe|interromper)\s*[.!]?\s*", clean_text):
            cancelled = await self.agent.cancel_active("operator_voice_or_chat")
            direct_response = "Interrompi o Agent Run ativo." if cancelled else "Não há Agent Run ativo para interromper."
        if self.shell is not None:
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
        await self.avatar.mode("thinking", state.value)
        await self.event_bus.publish(EventType.LLM_PROCESSING, state=state.value, response_id=response_id, turn_id=turn_id)
        await self.event_bus.publish(EventType.LLM_STREAM_STARTED, response_id=response_id, provider=self.llm.name, turn_id=turn_id)
        self.telemetry.mark(response_id, "t_llm_stream_started")

        config = self.settings_manager.value.realtime
        assembler = SentenceAssembler(config.minimum_chunk_characters, config.minimum_chunk_words, config.chunk_timeout_ms)
        speech_input: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        audio_urls: list[str] = []
        speech_degraded = asyncio.Event()
        speech_task = asyncio.create_task(
            self._speech_worker(response_id, turn_id, state.value, speech_input, audio_urls, cancel_event, speech_degraded),
            name=f"nyra-speech-stream-{response_id[:8]}",
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
            if speech_task:
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
        if not response:
            raise RuntimeError("LLM returned an empty response")
        await self.memory.add(MemoryCreate(category=MemoryCategory.SHORT_TERM, role="assistant", content=response, importance=5))
        await self.memory.retain()
        prepared = self.prosody.prepare(response, provider=self.tts.name)
        await self.event_bus.publish(
            EventType.NYRA_RESPONSE, response_id=response_id, turn_id=turn_id, text=response,
            display_text=prepared.display_text, speech_text=prepared.speech_text, state=state.value,
        )
        self.telemetry.mark(response_id, "t_response_complete")
        metrics = self.telemetry.finish(response_id)
        logger.info(
            "PERF request=%s stt=%sms memory=%sms context=%sms tools=%sms prompt_chars=%s ollama_connect=%sms ollama_first_token=%sms ollama_generation=%sms ollama_total=%sms tts_first_audio=%sms total=%sms",
            response_id,
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
        await self.avatar.mode("idle", state.value)
        return ChatResult(
            response_id=response_id, turn_id=turn_id, pipeline_status=final_status.value, response=response,
            display_text=prepared.display_text, speech_text=prepared.speech_text, state=state.value,
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
        if speech_task:
            await queue.put((index, styled))
        return index + 1

    async def _speech_worker(self, response_id: str, turn_id: str, state: str, queue: asyncio.Queue,
                             audio_urls: list[str], cancel_event: asyncio.Event,
                             degraded: asyncio.Event) -> None:
        first = True
        while True:
            item = await queue.get()
            if item is None:
                break
            index, sentence = item
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if response_id in self._speech_cancelled_ids:
                return
            prepared = self.prosody.prepare(sentence, provider=self.tts.name)
            if first:
                await self.event_bus.publish(EventType.TTS_STARTED, state=state, response_id=response_id, turn_id=turn_id, streaming=True)
                self.telemetry.mark(response_id, "t_tts_start")
            await self.event_bus.publish(EventType.TTS_CHUNK_STARTED, response_id=response_id, turn_id=turn_id, index=index, state=state)
            try:
                output = await self.speech_queue.synthesize(
                    self.tts, prepared.speech_text, state, SpeechPriority.USER,
                    response_id=response_id, chunk_index=index, turn_id=turn_id,
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
                await self.avatar.mode("speaking", state)
                first = False
            await self.event_bus.publish(
                EventType.TTS_CHUNK_FINISHED, response_id=response_id, turn_id=turn_id, index=index,
                audio_url=audio_url, state=state, display_text=sentence,
            )
        if not first:
            await self.event_bus.publish(EventType.TTS_FINISHED, state=state, response_id=response_id, turn_id=turn_id, streaming=True)

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
        response_id = self._active_response_id
        if not response_id:
            return False
        self._speech_cancelled_ids.add(response_id)
        cancelled = await self.speech_queue.cancel(response_id)
        await self.event_bus.publish(EventType.USER_INTERRUPTED, response_id=response_id, reason=reason, speech_only=True)
        await self.event_bus.publish(EventType.SPEECH_CANCELLED, response_id=response_id, reason=reason)
        await self._set_status(RealtimeStatus.INTERRUPTED, response_id)
        await self.avatar.mode("listening", "neutral")
        await self._set_status(RealtimeStatus.LISTENING, response_id)
        return cancelled > 0 or self.status == RealtimeStatus.LISTENING

    async def _cancelled(self, response_id: str, reason: str) -> None:
        self.telemetry.record("USER_INTERRUPTED", response_id=response_id, reason=reason)
        await self._set_status(RealtimeStatus.INTERRUPTED, response_id)
        await self.event_bus.publish(EventType.USER_INTERRUPTED, response_id=response_id, reason=reason)
        await self.event_bus.publish(EventType.SPEECH_CANCELLED, response_id=response_id, reason=reason)
        await self.event_bus.publish(EventType.REALTIME_CANCELLED, response_id=response_id, reason=reason)
        await self.avatar.mode("listening", "neutral")
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
            "turns": self.turns.snapshot(),
        }
