from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from datetime import datetime, timezone

from app.character.state import EmotionalState, StateMachine
from app.core.config import Settings
from app.events import Event, EventBus, EventType
from app.integrations.sentinel.connector import SentinelConnector
from app.integrations.sentinel.models import SentinelEvent, SentinelSeverity
from app.memory.models import MemoryCategory, MemoryCreate
from app.speech.prosody import ProsodyProcessor
from app.speech.queue import SpeechPriority, SpeechQueue


logger = logging.getLogger("nyra.sentinel_bridge")


class ProactiveSentinelAlerts:
    def __init__(self, settings: Settings, event_bus: EventBus, state_machine: StateMachine,
                 speech_queue: SpeechQueue, provider_getter, connector: SentinelConnector, memory=None,
                 voice_processor=None) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.state_machine = state_machine
        self.speech_queue = speech_queue
        self.provider_getter = provider_getter
        self.connector = connector
        self.memory = memory
        self.voice_processor = voice_processor
        self.prosody = ProsodyProcessor()
        self._cooldowns: dict[str, float] = {}
        self._spoken_incidents: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._disconnect_task: asyncio.Task | None = None
        self._disconnect_announced = False
        self._voice_buffer: list[tuple[SentinelEvent, str, EmotionalState]] = []
        self._voice_buffer_lock = asyncio.Lock()
        self._voice_flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.event_bus.subscribe(self.handle_event)

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.handle_event)
        if self._disconnect_task:
            self._disconnect_task.cancel()
        if self._voice_flush_task:
            self._voice_flush_task.cancel()
        for task in tuple(self._tasks):
            task.cancel()

    async def handle_event(self, event: Event) -> None:
        if event.type == EventType.SENTINEL_STATUS_CHANGED:
            await self._handle_status(event)
            return
        if event.type != EventType.SENTINEL_EVENT:
            return
        task = asyncio.create_task(self._process(event), name="nyra-sentinel-alert")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_status(self, event: Event) -> None:
        state = str(event.payload.get("state") or "")
        previous = str(event.payload.get("previous") or "")
        if state == "CONNECTED":
            if self._disconnect_task:
                self._disconnect_task.cancel()
                self._disconnect_task = None
            if self._disconnect_announced:
                self._disconnect_announced = False
                await self._process(self._synthetic_event(
                    "sentinel_recovered", "recovery", "Consegui reconectar ao Sentinel."
                ))
            return
        if previous == "CONNECTED" and state == "RECONNECTING":
            if self._disconnect_task:
                self._disconnect_task.cancel()
            self._disconnect_task = asyncio.create_task(self._announce_disconnect_after_grace())

    async def _announce_disconnect_after_grace(self) -> None:
        try:
            await asyncio.sleep(self.settings.sentinel_disconnect_grace_seconds)
            if self.connector.state.value not in {"CONNECTED", "DISABLED"}:
                self._disconnect_announced = True
                await self._process(self._synthetic_event(
                    "sentinel_offline", "warning",
                    "O Sentinel parou de responder. Vou continuar tentando reconectar.",
                ))
        except asyncio.CancelledError:
            return

    @staticmethod
    def _synthetic_event(event_type: str, severity: str, summary: str) -> Event:
        payload = {
            "schema_version": 1,
            "event_id": f"integration-{event_type}-{time.time_ns()}",
            "source": "utamo-sentinel",
            "instance_id": "nyra-integration-state",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "category": "integration",
            "type": event_type,
            "severity": severity,
            "title": "Conexão com o Sentinel",
            "summary": summary,
            "entity": {"type": "integration", "name": "Utamo Sentinel"},
            "metadata": {},
        }
        return Event(type=EventType.SENTINEL_EVENT, payload={"event": payload, "replay": False})

    async def _process(self, bus_event: Event) -> None:
        try:
            event = SentinelEvent.model_validate(bus_event.payload.get("event"))
        except Exception:
            return
        message = self._message(event)
        state = EmotionalState.NEUTRAL if event.severity == SentinelSeverity.RECOVERY else EmotionalState.CONCERNED
        await self.state_machine.transition(state)
        await self.event_bus.publish(
            EventType.SENTINEL_ALERT,
            message=message, display_text=message, severity=event.severity.value,
            event_id=event.event_id, desktop=self.settings.sentinel_desktop_alerts,
        )
        if (
            self.memory is not None
            and self.settings.sentinel_create_episodic_memory
            and event.severity == SentinelSeverity.CRITICAL
            and not bus_event.payload.get("replay")
        ):
            await self.memory.add(MemoryCreate(
                category=MemoryCategory.EPISODIC,
                content=f"Utamo Sentinel: {event.summary}",
                importance=8,
            ))
        if bus_event.payload.get("replay") or not self.settings.sentinel_voice_alerts:
            return
        if self.settings.sentinel_critical_only and event.severity not in {SentinelSeverity.CRITICAL, SentinelSeverity.RECOVERY}:
            return
        if event.severity == SentinelSeverity.INFO:
            return
        if self.settings.network_quiet_mode:
            return
        key = self._cooldown_key(event)
        now = time.monotonic()
        last_spoken = self._cooldowns.get(key)
        if last_spoken is not None and now - last_spoken < self.settings.sentinel_alert_cooldown_seconds:
            return
        incident_key = self._incident_key(event)
        if event.severity == SentinelSeverity.RECOVERY and incident_key not in self._spoken_incidents:
            return
        # The incident only becomes "spoken" after synthesis succeeds. This
        # keeps recovery chatter suppressed when an earlier alert was only
        # displayed (quiet mode, provider outage, or Critical Only).
        await self._buffer_voice(event, message, state)

    async def _buffer_voice(self, event: SentinelEvent, message: str, state: EmotionalState) -> None:
        """Coalesce short bursts without hiding their individual UI/history entries."""
        async with self._voice_buffer_lock:
            self._voice_buffer.append((event, message, state))
            if self._voice_flush_task is None or self._voice_flush_task.done():
                self._voice_flush_task = asyncio.create_task(
                    self._flush_voice_buffer(), name="nyra-sentinel-voice-burst"
                )

    async def _flush_voice_buffer(self) -> None:
        try:
            await asyncio.sleep(0.65)
            async with self._voice_buffer_lock:
                pending = self._voice_buffer
                self._voice_buffer = []
            if len(pending) >= 4:
                await self._speak_aggregate(pending)
                return
            for event, message, state in pending:
                await self._speak(event, message, state)
        except asyncio.CancelledError:
            return
        finally:
            self._voice_flush_task = None

    async def _speak_aggregate(
        self, pending: list[tuple[SentinelEvent, str, EmotionalState]]
    ) -> None:
        critical = sum(item[0].severity == SentinelSeverity.CRITICAL for item in pending)
        warning = sum(item[0].severity == SentinelSeverity.WARNING for item in pending)
        if critical:
            detail = f", incluindo {critical} crítico" + ("s" if critical != 1 else "")
        elif warning:
            detail = f", sendo {warning} de atenção"
        else:
            detail = ""
        message = f"O Sentinel detectou {len(pending)} eventos em sequência{detail}. Os detalhes estão no painel."
        state = EmotionalState.CONCERNED
        severity = SentinelSeverity.CRITICAL if critical else SentinelSeverity.WARNING
        if await self._synthesize(message, state, severity):
            for event, _, _ in pending:
                if event.severity != SentinelSeverity.RECOVERY:
                    self._spoken_incidents.add(self._incident_key(event))
                self._cooldowns[self._cooldown_key(event)] = time.monotonic()
                await self.connector.history.mark_spoken(event.event_id)
            logger.info("sentinel_alert_burst_aggregated", extra={"event_count": len(pending)})

    async def _speak(self, event: SentinelEvent, message: str, state: EmotionalState) -> None:
        if await self._synthesize(message, state, event.severity):
            if event.severity != SentinelSeverity.RECOVERY:
                self._spoken_incidents.add(self._incident_key(event))
            self._cooldowns[self._cooldown_key(event)] = time.monotonic()
            await self.connector.history.mark_spoken(event.event_id)

    async def _synthesize(
        self, message: str, state: EmotionalState, severity: SentinelSeverity
    ) -> bool:
        provider = self.provider_getter()
        if not await provider.health():
            return False
        prepared = self.prosody.prepare(message, provider=provider.name)
        priority = SpeechPriority.CRITICAL if severity == SentinelSeverity.CRITICAL else SpeechPriority.WARNING
        try:
            await self.event_bus.publish(EventType.TTS_STARTED, state=state.value, proactive=True, source="sentinel")
            output = await self.speech_queue.synthesize(provider, prepared.speech_text, state.value, priority)
            if self.voice_processor and self.voice_processor.config.enabled:
                output = await self.voice_processor.process(output, state.value)
            await self.event_bus.publish(
                EventType.NYRA_RESPONSE, text=message, display_text=message,
                speech_text=prepared.speech_text, state=state.value, proactive=True, source="sentinel",
            )
            await self.event_bus.publish(
                EventType.TTS_FINISHED, state=state.value,
                audio_url=f"/api/audio/{Path(output).name}", proactive=True, source="sentinel",
            )
            return True
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("sentinel_proactive_tts_failed", extra={"error_type": type(exc).__name__})
            return False

    @staticmethod
    def _message(event: SentinelEvent) -> str:
        name = event.entity.name.strip()
        if event.severity == SentinelSeverity.RECOVERY:
            return f"Voltou. O Sentinel recuperou comunicação com {name}." if name else "O Sentinel marcou o serviço como recuperado."
        if event.severity == SentinelSeverity.CRITICAL:
            return f"Aurélio... recebi um alerta crítico do Sentinel. {event.summary}"
        if name and any(term in event.type for term in ("offline", "down", "unavailable")):
            return f"Aurélio... o Sentinel perdeu comunicação com {name}."
        return f"O Sentinel enviou um alerta. {event.summary}"

    @staticmethod
    def _incident_key(event: SentinelEvent) -> str:
        normalized = event.type
        for suffix in ("_offline", "_down", "_unavailable", "_recovered", "_recovery", "_online", "_up"):
            normalized = normalized.removesuffix(suffix)
        return f"{normalized}:{event.entity.name}"

    @staticmethod
    def _cooldown_key(event: SentinelEvent) -> str:
        return f"{event.type}:{event.entity.name}:{event.severity.value}"
