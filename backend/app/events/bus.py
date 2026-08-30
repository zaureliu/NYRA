from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import BaseModel, Field


class EventType(StrEnum):
    USER_SPEECH_STARTED = "USER_SPEECH_STARTED"
    USER_SPEECH_PARTIAL = "USER_SPEECH_PARTIAL"
    USER_SPEECH_FINAL = "USER_SPEECH_FINAL"
    USER_SPEECH_RECEIVED = "USER_SPEECH_RECEIVED"
    STT_STARTED = "STT_STARTED"
    STT_COMPLETED = "STT_COMPLETED"
    USER_TEXT_RECEIVED = "USER_TEXT_RECEIVED"
    LLM_PROCESSING = "LLM_PROCESSING"
    LLM_STREAM_STARTED = "LLM_STREAM_STARTED"
    LLM_TOKEN_RECEIVED = "LLM_TOKEN_RECEIVED"
    SENTENCE_READY = "SENTENCE_READY"
    NYRA_RESPONSE = "NYRA_RESPONSE"
    TTS_STARTED = "TTS_STARTED"
    TTS_FINISHED = "TTS_FINISHED"
    TTS_CHUNK_STARTED = "TTS_CHUNK_STARTED"
    TTS_CHUNK_FINISHED = "TTS_CHUNK_FINISHED"
    TTS_CHUNK_FAILED = "TTS_CHUNK_FAILED"
    TTS_FAILED = "TTS_FAILED"
    PLAYBACK_STARTED = "PLAYBACK_STARTED"
    USER_INTERRUPTED = "USER_INTERRUPTED"
    SPEECH_CANCELLED = "SPEECH_CANCELLED"
    LISTENING_SETTINGS_CHANGED = "LISTENING_SETTINGS_CHANGED"
    MICROPHONE_STATE_CHANGED = "MICROPHONE_STATE_CHANGED"
    WAKE_WORD_DETECTED = "WAKE_WORD_DETECTED"
    HANDS_FREE_STARTED = "HANDS_FREE_STARTED"
    HANDS_FREE_ENDED = "HANDS_FREE_ENDED"
    NETWORK_GATEWAY_DOWN = "NETWORK_GATEWAY_DOWN"
    NETWORK_INTERNET_DOWN = "NETWORK_INTERNET_DOWN"
    NETWORK_DNS_FAILURE = "NETWORK_DNS_FAILURE"
    NETWORK_HIGH_LATENCY = "NETWORK_HIGH_LATENCY"
    NETWORK_PACKET_LOSS = "NETWORK_PACKET_LOSS"
    NETWORK_HIGH_JITTER = "NETWORK_HIGH_JITTER"
    NETWORK_INTERFACE_CHANGED = "NETWORK_INTERFACE_CHANGED"
    NETWORK_RECOVERED = "NETWORK_RECOVERED"
    NETWORK_STATUS_UPDATED = "NETWORK_STATUS_UPDATED"
    NETWORK_ALERT = "NETWORK_ALERT"
    SENTINEL_STATUS_CHANGED = "SENTINEL_STATUS_CHANGED"
    SENTINEL_EVENT = "SENTINEL_EVENT"
    SENTINEL_ALERT = "SENTINEL_ALERT"
    PC_ACTIVE_WINDOW_CHANGED = "PC_ACTIVE_WINDOW_CHANGED"
    MOUSE_ACTIVITY_CHANGED = "MOUSE_ACTIVITY_CHANGED"
    USER_IDLE = "USER_IDLE"
    USER_RETURNED = "USER_RETURNED"
    SYSTEM_LOAD_HIGH = "SYSTEM_LOAD_HIGH"
    PERCEPTION_UPDATED = "PERCEPTION_UPDATED"
    ATTENTION_CHANGED = "ATTENTION_CHANGED"
    SKILL_TRIGGERED = "SKILL_TRIGGERED"
    SHELL_EXECUTION_STARTED = "SHELL_EXECUTION_STARTED"
    SHELL_EXECUTION_FINISHED = "SHELL_EXECUTION_FINISHED"
    SHELL_APPROVAL_REQUIRED = "SHELL_APPROVAL_REQUIRED"
    SHELL_APPROVAL_DECIDED = "SHELL_APPROVAL_DECIDED"
    REMOTE_SHELL_EXECUTION_STARTED = "REMOTE_SHELL_EXECUTION_STARTED"
    REMOTE_SHELL_EXECUTION_FINISHED = "REMOTE_SHELL_EXECUTION_FINISHED"
    REMOTE_SHELL_APPROVAL_REQUIRED = "REMOTE_SHELL_APPROVAL_REQUIRED"
    AGENT_RUN_STARTED = "AGENT_RUN_STARTED"
    AGENT_RUN_STATE_CHANGED = "AGENT_RUN_STATE_CHANGED"
    AGENT_RUN_STEP = "AGENT_RUN_STEP"
    AGENT_RUN_FINISHED = "AGENT_RUN_FINISHED"
    AGENT_RUN_CANCELLED = "AGENT_RUN_CANCELLED"
    REACTION_TRIGGERED = "REACTION_TRIGGERED"
    AVATAR_STATE_CHANGED = "AVATAR_STATE_CHANGED"
    REALTIME_STATUS_CHANGED = "REALTIME_STATUS_CHANGED"
    REALTIME_CANCELLED = "REALTIME_CANCELLED"
    CONVERSATION_STATE_CHANGED = "CONVERSATION_STATE_CHANGED"
    OLLAMA_READINESS_CHANGED = "OLLAMA_READINESS_CHANGED"
    RUNTIME_STATUS_CHANGED = "RUNTIME_STATUS_CHANGED"
    RUNTIME_STARTING = "RUNTIME_STARTING"
    RUNTIME_RUNNING = "RUNTIME_RUNNING"
    RUNTIME_READY = "RUNTIME_READY"
    RUNTIME_STOPPING = "RUNTIME_STOPPING"
    RUNTIME_STOPPED = "RUNTIME_STOPPED"
    RUNTIME_RESTARTING = "RUNTIME_RESTARTING"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    RUNTIME_HEALTH_PASSED = "RUNTIME_HEALTH_PASSED"
    RUNTIME_HEALTH_FAILED = "RUNTIME_HEALTH_FAILED"
    RUNTIME_CRASH_LOOP = "RUNTIME_CRASH_LOOP"
    RUNTIME_RECOVERED = "RUNTIME_RECOVERED"
    DESKTOP_APP_LAUNCHED = "DESKTOP_APP_LAUNCHED"
    DESKTOP_WINDOW_VERIFIED = "DESKTOP_WINDOW_VERIFIED"
    UI_COMMAND = "UI_COMMAND"
    HOMELAB_EVENT = "HOMELAB_EVENT"
    HOMELAB_HOST_ONLINE = "HOMELAB_HOST_ONLINE"
    HOMELAB_HOST_OFFLINE = "HOMELAB_HOST_OFFLINE"
    HOMELAB_HOST_DEGRADED = "HOMELAB_HOST_DEGRADED"
    PROXMOX_VM_CHANGED = "PROXMOX_VM_CHANGED"
    PROXMOX_TASK_COMPLETED = "PROXMOX_TASK_COMPLETED"
    PROXMOX_TASK_FAILED = "PROXMOX_TASK_FAILED"
    HOME_ASSISTANT_ACTION_VERIFIED = "HOME_ASSISTANT_ACTION_VERIFIED"
    MEMORY_CREATED = "MEMORY_CREATED"
    STATE_CHANGED = "STATE_CHANGED"
    ERROR = "ERROR"
    # Operator V2 (prompt9)
    DESKTOP_EVENT = "DESKTOP_EVENT"
    MODAL_DETECTED = "MODAL_DETECTED"
    JOB_STARTED = "JOB_STARTED"
    JOB_FINISHED = "JOB_FINISHED"
    JOB_CANCELLED = "JOB_CANCELLED"
    TASK_STARTED = "TASK_STARTED"
    TASK_STATE_CHANGED = "TASK_STATE_CHANGED"
    TASK_FINISHED = "TASK_FINISHED"
    WORKFLOW_TRIGGERED = "WORKFLOW_TRIGGERED"
    WORKFLOW_FINISHED = "WORKFLOW_FINISHED"
    WATCH_REGISTERED = "WATCH_REGISTERED"
    WATCH_EXPIRED = "WATCH_EXPIRED"
    ELEVATED_SESSION_OPENED = "ELEVATED_SESSION_OPENED"
    ELEVATED_SESSION_CLOSED = "ELEVATED_SESSION_CLOSED"
    CREDENTIAL_CHANGED = "CREDENTIAL_CHANGED"
    RECOVERY_EXECUTED = "RECOVERY_EXECUTED"
    PROACTIVE_ALERT_FIRED = "PROACTIVE_ALERT_FIRED"
    MONITOR_JOB_CREATED = "MONITOR_JOB_CREATED"
    MONITOR_JOB_READING = "MONITOR_JOB_READING"
    MONITOR_JOB_CHANGED = "MONITOR_JOB_CHANGED"
    MONITOR_JOB_COMPLETED = "MONITOR_JOB_COMPLETED"
    MONITOR_JOB_FAILED = "MONITOR_JOB_FAILED"
    MONITOR_JOB_CANCELLED = "MONITOR_JOB_CANCELLED"
    MONITOR_NOTIFICATION = "MONITOR_NOTIFICATION"
    # Windows USB Monitor V1: eventos lógicos já deduplicados.
    USB_MONITOR_STARTED = "usb.monitor.started"
    USB_MONITOR_STOPPED = "usb.monitor.stopped"
    USB_DEVICE_CONNECTED = "usb.device.connected"
    USB_DEVICE_DISCONNECTED = "usb.device.disconnected"
    USB_DEVICE_UNKNOWN = "usb.device.unknown"
    USB_DEVICE_KNOWN_CONNECTED = "usb.device.known_connected"
    USB_DEVICE_REGISTERED = "usb.device.registered"
    USB_DEVICE_FORGOTTEN = "usb.device.forgotten"
    USB_DEVICE_COM_CHANGED = "usb.device.com_changed"
    USB_DEVICE_METADATA_CHANGED = "usb.device.metadata_changed"
    USB_DEVICE_IDENTITY_CHANGED = "usb.device.identity_changed"
    USB_MONITOR_FAILURE = "usb_monitor_failure"
    USB_RESOLVER_FAILURE = "usb_resolver_failure"
    USB_EVENT_DUPLICATE_RATE = "usb_event_duplicate_rate"
    USB_UNKNOWN_RESOLUTION_FAILURE = "usb_unknown_resolution_failure"
    # nyra-7c §16/§79: eventos normalizados das camadas de autonomia
    COMPUTER_WINDOW_FOREGROUND_CHANGED = "computer.window.foreground_changed"
    COMPUTER_WINDOW_OPENED = "computer.window.opened"
    COMPUTER_WINDOW_CLOSED = "computer.window.closed"
    COMPUTER_PROCESS_STARTED = "computer.process.started"
    COMPUTER_PROCESS_STOPPED = "computer.process.stopped"
    COMPUTER_FILE_CREATED = "computer.file.created"
    COMPUTER_FILE_MODIFIED = "computer.file.modified"
    COMPUTER_CLIPBOARD_CHANGED = "computer.clipboard.changed"
    COMPUTER_APPLICATION_LAUNCHED = "computer.application.launched"
    COMPUTER_APPLICATION_CLOSED = "computer.application.closed"
    COMPUTER_BROWSER_NAVIGATION = "computer.browser.navigation"
    COMPUTER_DIALOG_DETECTED = "computer.dialog.detected"
    COMPUTER_STATE_UPDATED = "computer.state.updated"
    COMPUTER_INTENT_RESOLVED = "computer.intent.resolved"
    COMPUTER_ACTION_EXECUTED = "computer.action.executed"
    COMPUTER_EFFECT_VERIFIED = "computer.effect.verified"
    USAGE_PATTERN_DETECTED = "usage.pattern.detected"
    SKILL_CANDIDATE_CREATED = "skill.candidate.created"
    SKILL_LEARNED = "skill.learned"
    SKILL_EXECUTED = "skill.executed"
    SKILL_DEGRADED = "skill.degraded"
    # nyra-7c §103: sinais passivos para um Self-Development futuro. Estes
    # eventos não disparam correção, patch ou execução; são só telemetria.
    COMPUTER_PERCEPTION_FAILURE = "perception_failure"
    COMPUTER_INTENT_RESOLUTION_FAILURE = "intent_resolution_failure"
    COMPUTER_CONTEXT_RESOLUTION_FAILURE = "context_resolution_failure"
    COMPUTER_OPERATOR_FAILURE = "operator_failure"
    COMPUTER_VERIFICATION_FAILURE = "verification_failure"
    COMPUTER_USAGE_PATTERN_FAILURE = "usage_pattern_failure"
    COMPUTER_SKILL_EXECUTION_FAILURE = "skill_execution_failure"
    # Self-Development Engine V1: audit events contain identifiers/status only.
    SELFDEV_ISSUE_DETECTED = "selfdev.issue.detected"
    SELFDEV_PLAN_CREATED = "selfdev.plan.created"
    SELFDEV_WORKTREE_CREATED = "selfdev.worktree.created"
    SELFDEV_PATCH_READY = "selfdev.patch.ready"
    SELFDEV_VALIDATION_PASS = "selfdev.validation.pass"
    SELFDEV_VALIDATION_FAIL = "selfdev.validation.fail"
    SELFDEV_PROMOTION_APPLIED = "selfdev.promotion.applied"
    SELFDEV_POST_VALIDATION_PASS = "selfdev.post_validation.pass"
    SELFDEV_ROLLBACK = "selfdev.rollback"
    SELFDEV_GITHUB_PUSHED = "selfdev.github.pushed"
    SELFDEV_GITHUB_BLOCKED = "selfdev.github.blocked"


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    seq: int = 0  # sequência monotônica global (prompt11 Parte AC §158)
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    #: Backpressure (Parte 5.2): um assinante lento não pode monopolizar o
    #: publisher. Cada notificação tem prazo blindado — estourou, o handler
    #: continua em background (shield) e o publish segue sem bloquear o turno.
    SUBSCRIBER_TIMEOUT_SECONDS = 0.5

    def __init__(self, history_size: int = 100) -> None:
        self._subscribers: set[Subscriber] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()
        self._sequence: int = 0
        self._detached: set[asyncio.Task[None]] = set()
        self.counters = {"events_published": 0, "subscriber_timeouts": 0}

    async def subscribe(self, subscriber: Subscriber) -> None:
        async with self._lock:
            self._subscribers.add(subscriber)

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        async with self._lock:
            self._subscribers.discard(subscriber)

    async def publish(self, event_type: EventType, **payload: Any) -> Event:
        # §158: sequence id monotônico — a UI ignora eventos com seq <= visto.
        self._sequence += 1
        event = Event(type=event_type, payload=payload, seq=self._sequence)
        self._history.append(event)
        self.counters["events_published"] += 1
        subscribers = tuple(self._subscribers)
        if subscribers:
            await asyncio.gather(
                *(self._safe_notify(callback, event) for callback in subscribers)
            )
        return event

    async def _safe_notify(self, callback: Subscriber, event: Event) -> None:
        async def _run() -> None:
            try:
                await callback(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        task = asyncio.create_task(_run())

        def _finish(finished: asyncio.Task[None]) -> None:
            self._detached.discard(finished)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(_finish)
        timed_out, _pending = await asyncio.wait({task}, timeout=self.SUBSCRIBER_TIMEOUT_SECONDS)
        if not timed_out:
            # Handler lento segue rodando isolado; o pipeline não espera.
            self.counters["subscriber_timeouts"] += 1
            self._detached.add(task)

    def detached_handler_tasks(self) -> tuple[asyncio.Task[None], ...]:
        """Handlers que estouraram o prazo e seguem em background."""
        return tuple(self._detached)

    def stats(self) -> dict[str, int]:
        return dict(self.counters)

    def history(self) -> list[Event]:
        return list(self._history)
