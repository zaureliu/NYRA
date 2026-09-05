from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.character.state import EmotionalState, StateMachine
from app.core.config import Settings
from app.events import Event, EventBus, EventType
from app.speech.prosody import ProsodyProcessor
from app.speech.queue import SpeechPriority, SpeechQueue


logger = logging.getLogger("kazumi.network_watch")
NETWORK_EVENT_TYPES = {
    EventType.NETWORK_GATEWAY_DOWN,
    EventType.NETWORK_GATEWAY_RECOVERED,
    EventType.NETWORK_INTERNET_DOWN,
    EventType.NETWORK_INTERNET_RECOVERED,
    EventType.NETWORK_DNS_FAILURE,
    EventType.NETWORK_DNS_RECOVERED,
    EventType.NETWORK_HIGH_LATENCY,
    EventType.NETWORK_LATENCY_RECOVERED,
    EventType.NETWORK_PACKET_LOSS,
    EventType.NETWORK_PACKET_LOSS_RECOVERED,
    EventType.NETWORK_HIGH_JITTER,
    EventType.NETWORK_JITTER_RECOVERED,
    EventType.NETWORK_INTERFACE_CHANGED,
    EventType.NETWORK_LINK_DOWN,
    EventType.NETWORK_LINK_UP,
    EventType.NETWORK_RX_ERRORS_DETECTED,
    EventType.NETWORK_TX_ERRORS_DETECTED,
    EventType.NETWORK_DROPS_DETECTED,
    EventType.NETWORK_RECOVERED,
}


class ProactiveNetworkAlerts:
    """Decides whether a network event is visual-only or should be spoken."""

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        state_machine: StateMachine,
        speech_queue: SpeechQueue,
        provider_getter,
        voice_processor=None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.state_machine = state_machine
        self.speech_queue = speech_queue
        self.provider_getter = provider_getter
        self.voice_processor = voice_processor
        self.prosody = ProsodyProcessor()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.event_bus.subscribe(self.handle_event)

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.handle_event)
        for task in tuple(self._tasks):
            task.cancel()

    async def handle_event(self, event: Event) -> None:
        if event.type not in NETWORK_EVENT_TYPES:
            return
        task = asyncio.create_task(self._process(event), name="kazumi-network-alert")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, event: Event) -> None:
        payload = event.payload
        severity = str(payload.get("severity", "info"))
        message = str(payload.get("message", "Mudança detectada na conexão."))
        state = EmotionalState.NEUTRAL if severity == "recovery" else EmotionalState.CONCERNED
        await self.state_machine.transition(state)
        await self.event_bus.publish(
            EventType.NETWORK_ALERT,
            message=message,
            severity=severity,
            spoken=False,
            desktop=self.settings.network_desktop_alerts,
            metrics=payload.get("metrics", {}),
        )
        if not self.settings.network_voice_alerts:
            return
        if self.settings.network_quiet_mode and not (
            severity == "critical" and self.settings.network_critical_voice_in_quiet
        ):
            return
        if severity == "info":
            return
        provider = self.provider_getter()
        if not await provider.health():
            return
        prepared = self.prosody.prepare(message, provider=provider.name)
        priority = SpeechPriority.CRITICAL if severity == "critical" else SpeechPriority.WARNING
        try:
            await self.event_bus.publish(EventType.TTS_STARTED, state=state.value, proactive=True)
            output = await self.speech_queue.synthesize(provider, prepared.speech_text, state.value, priority)
            if self.voice_processor and self.voice_processor.config.enabled:
                output = await self.voice_processor.process(output, state.value)
            audio_url = f"/api/audio/{Path(output).name}"
            await self.event_bus.publish(
                EventType.KAZUMI_RESPONSE,
                text=message,
                display_text=message,
                speech_text=prepared.speech_text,
                state=state.value,
                proactive=True,
            )
            await self.event_bus.publish(
                EventType.TTS_FINISHED,
                state=state.value,
                audio_url=audio_url,
                proactive=True,
            )
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("proactive_tts_failed", extra={"error_type": type(exc).__name__})
