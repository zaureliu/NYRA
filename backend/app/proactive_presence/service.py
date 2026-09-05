"""Event-driven orchestration and presentation for Proactive Presence V1."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from typing import Any, Callable

from app.core.runtime_settings import save_runtime_settings
from app.events import Event, EventBus, EventType
from app.proactive_presence.decision import ProactiveDecisionEngine
from app.proactive_presence.models import (
    DecisionContext,
    DecisionRecord,
    ProactiveCandidate,
    ProactiveDecision,
    ProactiveMode,
    ProactiveNotification,
    ProactivePriority,
    ProactiveSettings,
    ProactiveSettingsUpdate,
)
from app.proactive_presence.policy import SUPPORTED_EVENTS, candidate_from_event
from app.proactive_presence.storage import ProactivePresenceStore
from app.speech.prosody import ProsodyProcessor
from app.speech.queue import SpeechPriority
from app.tools.redaction import redact_secrets


logger = logging.getLogger("kazumi.proactive_presence")


_RELEASE_EVENTS = {
    EventType.TTS_FINISHED, EventType.TTS_FAILED, EventType.SPEECH_CANCELLED,
    EventType.KAZUMI_RESPONSE, EventType.SHELL_EXECUTION_FINISHED,
    EventType.REMOTE_SHELL_EXECUTION_FINISHED, EventType.AGENT_RUN_FINISHED,
    EventType.USER_RETURNED, EventType.HANDS_FREE_ENDED,
}
_USER_EVENTS = {
    EventType.USER_TEXT_RECEIVED, EventType.USER_SPEECH_STARTED,
    EventType.USER_SPEECH_FINAL, EventType.USER_SPEECH_RECEIVED,
}


class ProactivePresenceService:
    """Consumes existing events and owns their contextual presentation.

    The service never invokes operational tools. A notification carries an
    explicit false authorization marker and consumes zero Action Budget.
    """

    QUEUE_LIMIT = 256

    def __init__(
        self,
        *,
        settings: Any,
        event_bus: EventBus,
        intelligence_store: Any,
        world_state: Any,
        open_loops: Any,
        speech_queue: Any = None,
        provider_getter: Callable[[], Any] | None = None,
        voice_processor: Any = None,
        persona_runtime: Any = None,
        emotional_presence: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runtime_settings = settings
        self.event_bus = event_bus
        self.world_state = world_state
        self.open_loops = open_loops
        self.speech_queue = speech_queue
        self.provider_getter = provider_getter
        self.voice_processor = voice_processor
        self.persona_runtime = persona_runtime
        self.emotional_presence = emotional_presence
        self.clock = clock
        self.settings = self._settings_from_runtime()
        self.store = ProactivePresenceStore(intelligence_store, clock=clock)
        self.decision_engine = ProactiveDecisionEngine()
        self.prosody = ProsodyProcessor()
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        self._queued_ids: set[str] = set()
        self._runner: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._voice_tasks: set[asyncio.Task[None]] = set()
        self._process_lock = asyncio.Lock()
        self._started = False
        self._recent_user_at = 0.0
        self._decision_durations_ms: list[float] = []
        self.counters = {
            "events_received": 0,
            "fast_ignored": 0,
            "decisions_audited": 0,
            "presented": 0,
            "ignored": 0,
            "log_only": 0,
            "deferred": 0,
            "cooldown_suppressed": 0,
            "duplicates_coalesced": 0,
            "queue_dropped": 0,
            "voice_sent": 0,
            "voice_failed": 0,
            "processing_failures": 0,
            "spam_events": 0,
        }

    async def start(self) -> None:
        if self._started:
            return
        await self.event_bus.subscribe(self.handle_event)
        self._runner = asyncio.create_task(self._event_loop(), name="kazumi-proactive-presence")
        self._started = True
        self._schedule_deferred_flush(delay=.2)

    async def stop(self) -> None:
        if not self._started:
            return
        await self.event_bus.unsubscribe(self.handle_event)
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        for task in tuple(self._voice_tasks):
            task.cancel()
        if self._voice_tasks:
            await asyncio.gather(*self._voice_tasks, return_exceptions=True)
        self._voice_tasks.clear()
        self._flush_task = None
        self._runner = None
        self._started = False

    async def handle_event(self, event: Event) -> None:
        if event.type in _USER_EVENTS:
            self._recent_user_at = self.clock()
        if event.type in _RELEASE_EVENTS:
            self._schedule_deferred_flush()
        if event.type not in SUPPORTED_EVENTS:
            return
        self.counters["events_received"] += 1
        # NetworkWatch already turns sustained changes into explicit events.
        # Its frequent snapshot is known routine telemetry and must not create
        # queue/SQLite work or a user-facing message.
        if event.type == EventType.NETWORK_STATUS_UPDATED:
            self.counters["fast_ignored"] += 1
            self.counters["ignored"] += 1
            return
        if event.id in self._queued_ids:
            self.counters["duplicates_coalesced"] += 1
            return
        try:
            self._queue.put_nowait(event)
            self._queued_ids.add(event.id)
        except asyncio.QueueFull:
            self.counters["queue_dropped"] += 1

    async def evaluate_event(self, event: Event) -> DecisionRecord | None:
        """Synchronous evaluation hook for tests and controlled diagnostics."""
        if event.type not in SUPPORTED_EVENTS:
            return None
        await asyncio.sleep(.03)
        return await self._process(event)

    async def _event_loop(self) -> None:
        while True:
            event = await self._queue.get()
            self._queued_ids.discard(event.id)
            try:
                # Other subscribers (World State/Open Loops) consume the same
                # source event. This short yield lets their grounded snapshots
                # settle without coupling either engine to subscriber order.
                await asyncio.sleep(.03)
                await self._process(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.counters["processing_failures"] += 1
                logger.exception(
                    "proactive_presence_event_failed",
                    extra={"event_type": event.type.value, "error_type": type(exc).__name__},
                )
            finally:
                self._queue.task_done()

    async def _process(self, event: Event) -> DecisionRecord | None:
        async with self._process_lock:
            linked_loop = await self._linked_loop(event)
            candidate = candidate_from_event(event, linked_loop)
            if candidate is None:
                return None
            candidate = candidate.model_copy(update={
                "message": redact_secrets(candidate.message)[:500],
                "entity": redact_secrets(candidate.entity)[:240],
            })
            return await self._evaluate_candidate(candidate)

    async def _evaluate_candidate(
        self, candidate: ProactiveCandidate, *, from_deferred: bool = False,
    ) -> DecisionRecord:
        started = time.perf_counter()
        now = self.clock()
        cooldown_keys = self._cooldown_keys(candidate)
        cooldown_active = await self.store.any_cooldown(cooldown_keys, now)
        repeat_count = await self.store.note_occurrence(candidate.dedup_key, now)
        recovery_relevant = (
            await self.store.incident_open(candidate.recovery_of)
            if candidate.recovery_of else True
        )
        snapshot = self.world_state.get_snapshot() if self.world_state is not None else {}
        user_activity = str(self._slot(snapshot, "user_activity_state") or "UNKNOWN").upper()
        assistant_state = str(self._slot(snapshot, "assistant_state") or "IDLE").upper()
        relation_goal = self._goal_relation(candidate, snapshot)
        recent_relation = .8 if self._recent_user_at and now - self._recent_user_at <= 300 else 0.0
        age = max(0.0, now - candidate.occurred_at.timestamp())
        freshness = max(0.0, min(1.0, 1.0 - age / 3600))
        voice_ready = await self._voice_ready(candidate, assistant_state)
        context = DecisionContext(
            user_activity=user_activity,
            assistant_state=assistant_state,
            relation_to_active_goal=relation_goal,
            relation_to_recent_request=recent_relation,
            novelty=0.0 if cooldown_active else 1.0,
            repeat_count=max(0, repeat_count - 1),
            freshness=freshness,
            cooldown_active=cooldown_active,
            recovery_relevant=recovery_relevant,
            voice_ready=voice_ready,
            notifications_last_hour=await self.store.notifications_last_hour(),
        )
        record = self.decision_engine.decide(candidate, context, self.settings)
        await self.store.record_decision(record)
        self.counters["decisions_audited"] += 1

        if record.decision == ProactiveDecision.DEFER:
            deferred_candidate = candidate
            if record.reason == "hourly_presentation_budget":
                deferred_candidate = candidate.model_copy(update={
                    "metadata": {
                        **candidate.metadata,
                        "retry_not_before": now + min(300, self.settings.defer_ttl_seconds),
                    },
                })
            await self.store.defer(
                deferred_candidate, ttl_seconds=self.settings.defer_ttl_seconds,
            )
            self.counters["deferred"] += 1
        elif record.decision in {ProactiveDecision.IGNORE, ProactiveDecision.LOG_ONLY}:
            if from_deferred:
                await self.store.delete_deferred(candidate.dedup_key)
            if record.decision == ProactiveDecision.IGNORE:
                self.counters["ignored"] += 1
            else:
                self.counters["log_only"] += 1
            if record.reason == "semantic_cooldown_coalesced":
                self.counters["cooldown_suppressed"] += 1
                self.counters["duplicates_coalesced"] += 1
        else:
            if cooldown_active:
                # Defensive invariant: a cooldown-active candidate can never
                # become user-facing even if policy code changes later.
                self.counters["spam_events"] += 1
                raise RuntimeError("PROACTIVE_COOLDOWN_INVARIANT")
            await self._present(candidate, record, repeat_count)
            await self.store.consume_cooldowns(
                candidate.dedup_key,
                self._cooldown_scopes(candidate),
                now,
            )
            if candidate.opens_incident:
                await self.store.set_incident(candidate.opens_incident, is_open=True, notified=True, now=now)
            if candidate.recovery_of:
                await self.store.set_incident(candidate.recovery_of, is_open=False, now=now)
            if from_deferred:
                await self.store.delete_deferred(candidate.dedup_key)

        duration = (time.perf_counter() - started) * 1000
        self._decision_durations_ms = [*self._decision_durations_ms[-499:], duration]
        return record

    async def _present(self, candidate: ProactiveCandidate, record: DecisionRecord,
                       repeat_count: int) -> None:
        channels = {
            ProactiveDecision.UI_NOTIFICATION: ["ui"],
            ProactiveDecision.CHAT_MESSAGE: ["ui", "chat"],
            ProactiveDecision.VOICE_AND_CHAT: ["ui", "chat", "voice"],
        }[record.decision]
        message = candidate.message
        dialogue_policy = None
        emotion = None
        if self.persona_runtime is not None:
            message, dialogue, emotional = await self.persona_runtime.proactive_style(
                message,
                event_name=candidate.event_type,
                priority=candidate.priority.value,
            )
            dialogue_policy = dialogue.mode.value
            emotion = emotional.model_dump(mode="json")
        notification = ProactiveNotification(
            decision_id=record.decision_id,
            event_id=candidate.event_id,
            event_type=candidate.event_type,
            source=candidate.source,
            entity=candidate.entity,
            goal_id=candidate.goal_id,
            open_loop_id=candidate.open_loop_id,
            message=message,
            priority=candidate.priority,
            channels=channels,
            repeat_count=max(0, repeat_count - 1),
            execution_authorized=False,
            action_budget_consumed=0,
            dialogue_policy=dialogue_policy,
            emotion=emotion,
        )
        await self.store.save_notification(notification)
        self.counters["presented"] += 1
        await self.event_bus.publish(
            EventType.PROACTIVE_PRESENCE_NOTIFICATION,
            notification_id=notification.notification_id,
            decision_id=notification.decision_id,
            message=notification.message,
            priority=notification.priority.value,
            channels=notification.channels,
            source=notification.source,
            entity=notification.entity,
            goal_id=notification.goal_id,
            open_loop_id=notification.open_loop_id,
            repeat_count=notification.repeat_count,
            execution_authorized=False,
            action_budget_consumed=0,
            dialogue_policy=dialogue_policy,
            emotion=emotion,
        )
        if record.decision == ProactiveDecision.VOICE_AND_CHAT:
            task = asyncio.create_task(self._speak(notification), name="kazumi-proactive-voice")
            self._voice_tasks.add(task)
            task.add_done_callback(self._voice_tasks.discard)

    async def _speak(self, notification: ProactiveNotification) -> None:
        try:
            snapshot = self.world_state.get_snapshot() if self.world_state is not None else {}
            assistant = str(self._slot(snapshot, "assistant_state") or "IDLE").upper()
            if assistant in {"THINKING", "ACTING", "SPEAKING", "LISTENING"}:
                return
            provider = self.provider_getter() if self.provider_getter else None
            if provider is None or not await provider.health():
                self.counters["voice_failed"] += 1
                return
            prepared = self.prosody.prepare(notification.message, provider=provider.name)
            state = "concerned" if notification.priority.rank >= ProactivePriority.HIGH.rank else "neutral"
            voice_interface = None
            voice_build = None
            options = None
            if self.emotional_presence is not None:
                voice_build = self.emotional_presence.build_voice_style(
                    context={"source": "proactive_presence", "notification_id": notification.notification_id},
                )
                voice_interface = voice_build.presentation
                state = voice_interface.emotion.value
                options = voice_build.options
            elif self.persona_runtime is not None:
                capabilities = provider.capabilities() if callable(getattr(provider, "capabilities", None)) else None
                voice_interface = self.persona_runtime.voice_interface(
                    provider_supports_emotion=bool(getattr(capabilities, "supports_emotion", False)),
                )
                state = voice_interface.emotion.value
                from app.speech.emotion import EmotionPlan
                from app.speech.profile import load_voice_profile

                plan = EmotionPlan.validated(
                    state,
                    voice_interface.intensity,
                    confidence=1.0,
                    reason="persona_runtime_proactive",
                )
                _profile, defaults = load_voice_profile()
                options = defaults.with_emotion(plan)
            priority = (
                SpeechPriority.CRITICAL
                if notification.priority == ProactivePriority.CRITICAL
                else SpeechPriority.WARNING
            )
            await self.event_bus.publish(
                EventType.TTS_STARTED, state=state, proactive=True,
                source="proactive_presence", notification_id=notification.notification_id,
                response_id=notification.notification_id,
                voice_emotion=voice_interface.model_dump(mode="json") if voice_interface else None,
                voice_style=voice_build.presentation.model_dump(mode="json") if voice_build else None,
            )
            if options is None:
                output = await self.speech_queue.synthesize(
                    provider, prepared.speech_text, state, priority,
                    response_id=notification.notification_id,
                )
            else:
                output = await self.speech_queue.synthesize(
                    provider, prepared.speech_text,
                    voice_interface.acoustic_emotion if voice_interface else state,
                    priority, options=options, response_id=notification.notification_id,
                )
            if self.voice_processor and self.voice_processor.config.enabled:
                output = await self.voice_processor.process(output, state)
            await self.event_bus.publish(
                EventType.TTS_FINISHED, state=state,
                audio_url=f"/api/audio/{Path(output).name}",
                proactive=True, source="proactive_presence",
                response_id=notification.notification_id,
                notification_id=notification.notification_id,
            )
            self.counters["voice_sent"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self.counters["voice_failed"] += 1

    async def _voice_ready(self, candidate: ProactiveCandidate,
                           assistant_state: str) -> bool:
        if (
            not self.settings.voice_enabled
            or candidate.priority.rank < ProactivePriority.HIGH.rank
            or assistant_state in {"THINKING", "ACTING", "SPEAKING", "LISTENING"}
            or self.provider_getter is None
            or self.speech_queue is None
        ):
            return False
        try:
            provider = self.provider_getter()
            return bool(provider is not None and await provider.health())
        except Exception:
            return False

    async def _linked_loop(self, event: Event) -> Any:
        if self.open_loops is None:
            return None
        payload = event.payload if isinstance(event.payload, dict) else {}
        loop_id = str(payload.get("loop_id") or "")
        if loop_id:
            return await self.open_loops.get(loop_id)
        monitor_id = str(payload.get("monitor_id") or "")
        task_id = str(payload.get("task_id") or payload.get("agent_run_id") or "")
        artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
        artifact_id = str(artifact.get("artifact_id") or "")
        path = str(artifact.get("path") or "")
        if not any((monitor_id, task_id, artifact_id, path)):
            return None
        attempts = 3 if monitor_id and event.type in {
            EventType.MONITOR_JOB_COMPLETED,
            EventType.MONITOR_JOB_FAILED,
            EventType.MONITOR_JOB_CHANGED,
        } else 1
        for attempt in range(attempts):
            loops = await self.open_loops.list(limit=300)
            for loop in loops:
                if monitor_id and monitor_id in loop.related_monitor:
                    return loop
                if task_id and task_id in loop.related_task:
                    return loop
                if artifact_id and any(item.artifact_id == artifact_id for item in loop.related_artifact):
                    return loop
                if path and any(item.path.casefold() == path.casefold() for item in loop.related_artifact):
                    return loop
            if attempt + 1 < attempts:
                # Open Loops consumes the same source event on its own queue.
                # A bounded retry joins the structured relation without making
                # subscriber order part of either engine's contract.
                await asyncio.sleep(.04)
        return None

    def _schedule_deferred_flush(self, *, delay: float = .06) -> None:
        if not self._started:
            return
        if self._flush_task and not self._flush_task.done():
            return

        async def run() -> None:
            await asyncio.sleep(delay)
            await self.flush_deferred()

        self._flush_task = asyncio.create_task(run(), name="kazumi-proactive-deferred")

    async def flush_deferred(self) -> int:
        snapshot = self.world_state.get_snapshot() if self.world_state is not None else {}
        assistant = str(self._slot(snapshot, "assistant_state") or "IDLE").upper()
        user = str(self._slot(snapshot, "user_activity_state") or "UNKNOWN").upper()
        if assistant in {"THINKING", "ACTING", "SPEAKING", "LISTENING"} or user in {"SPEAKING", "LISTENING"}:
            return 0
        values = await self.store.deferred()
        processed = 0
        for candidate in values:
            retry_not_before = float(candidate.metadata.get("retry_not_before") or 0)
            if retry_not_before > self.clock():
                continue
            record = await self._evaluate_candidate(candidate, from_deferred=True)
            if record.decision != ProactiveDecision.DEFER:
                processed += 1
        return processed

    async def update(self, payload: ProactiveSettingsUpdate) -> ProactiveSettings:
        changes = payload.model_dump(exclude_none=True)
        updates: dict[str, Any] = {}
        if "enabled" in changes:
            updates["proactive_presence_enabled"] = bool(changes["enabled"])
        if "mode" in changes:
            mode = changes["mode"]
            updates["proactive_presence_mode"] = mode.value if isinstance(mode, ProactiveMode) else str(mode)
        if "voice_enabled" in changes:
            updates["proactive_voice_enabled"] = bool(changes["voice_enabled"])
        for key, value in updates.items():
            setattr(self.runtime_settings, key, value)
        if updates:
            await asyncio.to_thread(save_runtime_settings, updates)
        self.settings = self._settings_from_runtime()
        return self.settings

    def refresh_settings(self) -> ProactiveSettings:
        self.settings = self._settings_from_runtime()
        return self.settings

    async def status(self) -> dict[str, Any]:
        counts = await self.store.counts()
        durations = self._decision_durations_ms
        average = round(sum(durations) / len(durations), 4) if durations else 0.0
        p95 = round(sorted(durations)[max(0, int(len(durations) * .95) - 1)], 4) if durations else 0.0
        return {
            "state": "READY" if self._started else "OFFLINE",
            "settings": self.settings.model_dump(mode="json"),
            "queue": {"size": self._queue.qsize(), "limit": self.QUEUE_LIMIT},
            "counters": dict(self.counters),
            "storage": counts,
            "performance": {
                "average_decision_ms": average,
                "p95_decision_ms": p95,
                "samples": len(durations),
                "bounded_resident_items": self._queue.qsize() + len(self._queued_ids) + len(self._voice_tasks),
            },
            "execution_authorized": False,
            "action_budget_consumed": 0,
        }

    def _settings_from_runtime(self) -> ProactiveSettings:
        return ProactiveSettings(
            enabled=bool(getattr(self.runtime_settings, "proactive_presence_enabled", True)),
            mode=str(getattr(self.runtime_settings, "proactive_presence_mode", "NORMAL")).upper(),
            voice_enabled=bool(getattr(self.runtime_settings, "proactive_voice_enabled", False)),
            default_cooldown_seconds=int(getattr(
                self.runtime_settings, "proactive_presence_cooldown_seconds", 300,
            )),
            max_notifications_per_hour=int(getattr(
                self.runtime_settings, "proactive_presence_max_per_hour", 6,
            )),
            defer_ttl_seconds=int(getattr(
                self.runtime_settings, "proactive_presence_defer_ttl_seconds", 1800,
            )),
        )

    @staticmethod
    def _slot(snapshot: dict[str, Any], key: str) -> Any:
        value = snapshot.get(key)
        return value.get("value") if isinstance(value, dict) else value

    @staticmethod
    def _goal_relation(candidate: ProactiveCandidate, snapshot: dict[str, Any]) -> float:
        most = ProactivePresenceService._slot(snapshot, "most_relevant_open_loop")
        if isinstance(most, dict) and candidate.open_loop_id and most.get("id") == candidate.open_loop_id:
            return 1.0
        active_goal = ProactivePresenceService._slot(snapshot, "active_goal")
        if candidate.goal_id and active_goal:
            return .75
        return 0.0

    def _cooldown_seconds(self, candidate: ProactiveCandidate) -> float:
        default = float(self.settings.default_cooldown_seconds)
        if candidate.priority == ProactivePriority.CRITICAL:
            return min(default, 60.0)
        if candidate.source == "usb_monitor":
            return max(default, 3600.0)
        if candidate.source in {"tasks", "monitor_job", "selfdev"}:
            return min(default, 180.0)
        return default

    def _cooldown_keys(self, candidate: ProactiveCandidate) -> list[str]:
        priority = candidate.priority.value
        semantic = f"semantic:{candidate.dedup_key}"
        entity = f"entity:{candidate.source}:{candidate.entity.casefold()}:{priority}"
        # A recovery must not be hidden by the outage it closes. Critical
        # events only coalesce repeats of the same critical entity, never an
        # unrelated critical signal from the same source.
        if candidate.recovery_of or candidate.priority == ProactivePriority.CRITICAL:
            return [semantic, entity]
        keys = [
            semantic,
            f"type:{candidate.event_type}:{priority}",
            entity,
            f"source:{candidate.source}:{priority}",
        ]
        if candidate.goal_id:
            keys.append(f"goal:{candidate.goal_id}:{priority}")
        return keys

    def _cooldown_scopes(self, candidate: ProactiveCandidate) -> dict[str, float]:
        duration = self._cooldown_seconds(candidate)
        priority = candidate.priority.value
        scopes = {
            f"semantic:{candidate.dedup_key}": duration,
            f"entity:{candidate.source}:{candidate.entity.casefold()}:{priority}": duration,
        }
        if candidate.recovery_of or candidate.priority == ProactivePriority.CRITICAL:
            return scopes
        scopes[f"type:{candidate.event_type}:{priority}"] = min(duration, 60.0)
        scopes[f"source:{candidate.source}:{priority}"] = min(duration, 5.0)
        if candidate.goal_id:
            scopes[f"goal:{candidate.goal_id}:{priority}"] = min(duration, 30.0)
        return scopes
