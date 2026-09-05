from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import re
import time
import unicodedata
from typing import Any

from app.events import EventBus, EventType
from app.usb.discovery import (
    WindowsDeviceNotificationSource,
    WindowsUsbDiscovery,
    classify_relevance,
)
from app.usb.models import (
    DeviceRelevance,
    UsbDeviceObservation,
    UsbDeviceRecord,
    UsbHistoryEvent,
    UsbMonitorState,
    utc_now,
)
from app.usb.registry import UsbDeviceRegistry

logger = logging.getLogger("kazumi.usb.service")


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(char for char in normalized if not unicodedata.combining(char))
                    .casefold().split())


class UsbDeviceService:
    """Native PnP monitor -> registry -> EventBus/state/proactive notifications."""

    def __init__(
        self,
        event_bus: EventBus,
        *,
        computer_state=None,
        registry: UsbDeviceRegistry | None = None,
        discovery=None,
        notification_source=None,
        reconciliation_interval_seconds: float = 30.0,
        debounce_seconds: float = 0.8,
    ) -> None:
        self.event_bus = event_bus
        self.computer_state = computer_state
        self.registry = registry or UsbDeviceRegistry()
        self.discovery = discovery or WindowsUsbDiscovery()
        self.notification_source = notification_source or WindowsDeviceNotificationSource()
        self.reconciliation_interval_seconds = max(5.0, reconciliation_interval_seconds)
        self.debounce_seconds = max(0.05, debounce_seconds)
        self.state = UsbMonitorState.STOPPED
        self.event_source = "WINDOWS_CONFIGMGR"
        self.fallback = "SETUPAPI_RECONCILIATION"
        self.last_error: str | None = None
        self.last_event: dict[str, Any] | None = None
        self.last_heartbeat_at: str | None = None
        self.started_at: str | None = None
        self._connected: dict[str, UsbDeviceRecord] = {}
        self._queue: asyncio.Queue[None] = asyncio.Queue(maxsize=64)
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconcile_lock = asyncio.Lock()
        self._stopping = False
        self._last_relevant_device_id: str | None = None
        self._last_hardware_device_id: str | None = None
        self.last_hardware_observation: dict[str, Any] | None = None
        self._native_active = False
        self._recovery_attempts = 0
        self._event_hints = 0
        self._logical_events = 0
        self._duplicate_signal_at = 0.0

    async def start(self) -> None:
        if self.state != UsbMonitorState.STOPPED:
            return
        self.state = UsbMonitorState.STARTING
        self.started_at = utc_now()
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        try:
            await self.registry.initialize()
            persisted = await self.registry.list_devices(
                connected=True, include_internal=True
            )
            self._connected = {record.device_id: record for record in persisted}
            observations = await asyncio.to_thread(self.discovery.enumerate)
            if getattr(self.discovery, "last_error", None):
                raise RuntimeError(str(self.discovery.last_error))
            await self._reconcile(observations, initial=True, reason="startup")
            self._native_active = bool(self.notification_source.start(self._native_hint))
            if self._native_active:
                self.state = UsbMonitorState.ACTIVE
                self.last_error = None
            else:
                self.state = UsbMonitorState.DEGRADED
                self.last_error = str(getattr(self.notification_source, "last_error", None)
                                      or "PNP_NOTIFICATION_UNAVAILABLE")
                await self._monitor_failure(self.last_error)
            self._spawn(self._event_worker(), "kazumi-usb-events")
            self._spawn(self._fallback_loop(), "kazumi-usb-reconciliation")
            await self.event_bus.publish(
                EventType.USB_MONITOR_STARTED,
                state=self.state.value,
                event_source=self.event_source if self._native_active else self.fallback,
                connected=len(self._connected),
                connected_devices=[
                    record.public_dict() for record in self._connected.values()
                    if record.relevance == DeviceRelevance.USER_RELEVANT
                ],
            )
            await self._sync_computer_state()
        except Exception as error:  # noqa: BLE001 - optional monitor must not stop KAZUMI
            self.state = UsbMonitorState.DEGRADED
            self.last_error = f"{type(error).__name__}: {error}"[:240]
            logger.exception("usb_monitor_start_failed")
            await self._monitor_failure(self.last_error)
            self._spawn(self._fallback_loop(), "kazumi-usb-reconciliation")
            await self._sync_computer_state()

    async def stop(self) -> None:
        if self.state == UsbMonitorState.STOPPED:
            return
        self._stopping = True
        self.notification_source.stop()
        self._native_active = False
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self.state = UsbMonitorState.STOPPED
        self.last_heartbeat_at = utc_now()
        await self._sync_computer_state()
        await self.event_bus.publish(EventType.USB_MONITOR_STOPPED, state=self.state.value)

    def _spawn(self, coroutine, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _native_hint(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or self._stopping:
            return
        loop.call_soon_threadsafe(self._enqueue_hint)

    def _enqueue_hint(self) -> None:
        self._event_hints += 1
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # One pending reconciliation already captures the final PnP state.
            pass

    async def _event_worker(self) -> None:
        while not self._stopping:
            await self._queue.get()
            await asyncio.sleep(self.debounce_seconds)
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            before = self._logical_events
            await self.refresh(reason="pnp_event")
            if self._logical_events == before:
                await self._maybe_duplicate_signal()

    async def _fallback_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.reconciliation_interval_seconds)
            if self._stopping:
                return
            await self.refresh(reason="reconciliation")
            if not self._native_active and self._recovery_attempts < 3:
                self._recovery_attempts += 1
                self._native_active = bool(self.notification_source.start(self._native_hint))
                if self._native_active:
                    self.state = UsbMonitorState.ACTIVE
                    self.last_error = None
                    await self._sync_computer_state()

    async def refresh(self, *, reason: str = "manual") -> dict[str, Any]:
        async with self._reconcile_lock:
            discovery_success = False
            try:
                observations = await asyncio.to_thread(self.discovery.enumerate)
                discovery_error = getattr(self.discovery, "last_error", None)
                if discovery_error:
                    raise RuntimeError(str(discovery_error))
                await self._reconcile(observations, initial=False, reason=reason)
                discovery_success = True
                self.last_heartbeat_at = utc_now()
                if self._native_active:
                    self.state = UsbMonitorState.ACTIVE
                    self.last_error = None
            except Exception as error:  # noqa: BLE001
                self.state = UsbMonitorState.DEGRADED
                self.last_error = f"{type(error).__name__}: {error}"[:240]
                logger.warning("usb_reconciliation_failed", extra={"reason": reason,
                                                                     "error_type": type(error).__name__})
                await self.event_bus.publish(
                    EventType.USB_RESOLVER_FAILURE, reason=reason, error=self.last_error
                )
                await self._monitor_failure(self.last_error)
            await self._sync_computer_state()
            snapshot = await self.status_snapshot()
            if reason == "hardware_grounding":
                snapshot.update(
                    discovery_success=discovery_success,
                    observed_at=utc_now() if discovery_success else None,
                    observed_devices=[record.public_dict() for record in self._connected.values()]
                    if discovery_success else [],
                )
            return snapshot

    async def _reconcile(self, observations: list[UsbDeviceObservation], *,
                         initial: bool, reason: str) -> None:
        current_ids = {item.device_id for item in observations}
        previous_connected = dict(self._connected)
        previous_ids = set(previous_connected)
        next_connected: dict[str, UsbDeviceRecord] = {}

        for observation in observations:
            prior_live = previous_connected.get(observation.device_id)
            new_connection = observation.device_id not in previous_ids
            identity_match = None
            if new_connection:
                identity_match = await self.registry.identity_changed_match(observation)
            record, previous_record = await self.registry.observe(
                observation,
                new_connection=new_connection,
                present_at_startup=initial,
                identity_changed=identity_match is not None,
            )
            next_connected[record.device_id] = record
            if record.relevance == DeviceRelevance.USER_RELEVANT:
                self._last_relevant_device_id = record.device_id

            if initial:
                if record.relevance == DeviceRelevance.SYSTEM_INTERNAL:
                    continue
                await self._history(
                    "PRESENT_AT_STARTUP", record,
                    f"{self._display_name(record)} presente na inicialização.", "INFO",
                )
                continue

            if record.relevance == DeviceRelevance.SYSTEM_INTERNAL:
                continue

            if new_connection:
                # Make the authoritative in-memory view match the event before
                # subscribers are resumed. EventBus delivery is synchronous.
                self._connected[record.device_id] = record
                await self._connected_event(record, previous_record, identity_match)
            elif prior_live is not None:
                await self._metadata_events(prior_live, record)

        for device_id in previous_ids - current_ids:
            previous = previous_connected.get(device_id)
            migrated_relevance = (
                classify_relevance(previous.name, previous.device_instance_id or "").value
                if initial and previous is not None else None
            )
            record = await self.registry.mark_disconnected(
                device_id, relevance=migrated_relevance
            )
            # Consumers reacting to the disconnect must not observe the device
            # as connected through status_snapshot().
            self._connected.pop(device_id, None)
            if (not initial and record is not None
                    and record.relevance == DeviceRelevance.USER_RELEVANT):
                await self._disconnected_event(record)

        self._connected = next_connected
        self.last_heartbeat_at = utc_now()
        world = getattr(self.computer_state, "world_state", None)
        if world is not None and getattr(self.discovery, "simulated", True) is False:
            world.ingest_usb_snapshot([record.public_dict() for record in self._connected.values()],
                                      source=getattr(self.discovery, "source", "unknown"))
        await self._sync_computer_state()

    async def _connected_event(self, record: UsbDeviceRecord,
                               previous: UsbDeviceRecord | None,
                               identity_match: UsbDeviceRecord | None) -> None:
        await self._publish(EventType.USB_DEVICE_CONNECTED, record)
        changed_com = bool(previous and previous.com_port and record.com_port
                           and previous.com_port != record.com_port)
        if identity_match is not None:
            message = (
                f"Dispositivo USB com identidade diferente detectado como {record.name}. "
                "O nome se parece com um dispositivo conhecido, mas o fingerprint não corresponde."
            )
            await self._publish(EventType.USB_DEVICE_IDENTITY_CHANGED, record,
                                previous_device_id=identity_match.device_id)
            await self._history(EventType.USB_DEVICE_IDENTITY_CHANGED.value, record,
                                message, "WARNING")
            await self._notify(record, message, level="WARNING", voice=True,
                               kind="identity_changed")
        elif changed_com:
            message = (
                f"{self._display_name(record)} reconectado. A porta mudou de "
                f"{previous.com_port} para {record.com_port}."
            )
            await self._publish(EventType.USB_DEVICE_COM_CHANGED, record,
                                previous_com=previous.com_port, current_com=record.com_port)
            await self._history(EventType.USB_DEVICE_COM_CHANGED.value, record,
                                message, "NOTICE")
            await self._notify(record, message, level="NOTICE", kind="com_changed")
        elif record.registered:
            message = self._known_connected_message(record)
            await self._publish(EventType.USB_DEVICE_KNOWN_CONNECTED, record)
            await self._history(EventType.USB_DEVICE_KNOWN_CONNECTED.value, record,
                                message, "INFO")
            await self._notify(record, message, level="INFO", kind="known_connected")
        else:
            message = self._unknown_message(record)
            await self._publish(EventType.USB_DEVICE_UNKNOWN, record)
            await self._history(EventType.USB_DEVICE_UNKNOWN.value, record,
                                message, "WARNING")
            await self._notify(record, message, level="WARNING", voice=True,
                               kind="unknown_storage" if record.category == "Armazenamento"
                               else "unknown")
            if record.identity_confidence.value == "LOW":
                await self.event_bus.publish(
                    EventType.USB_UNKNOWN_RESOLUTION_FAILURE,
                    device_id=record.device_id,
                    identity_basis=record.identity_basis,
                )
        self._logical_events += 1

    async def _metadata_events(self, previous: UsbDeviceRecord,
                               current: UsbDeviceRecord) -> None:
        if previous.com_port and current.com_port and previous.com_port != current.com_port:
            message = (
                f"{self._display_name(current)}: porta serial alterada de "
                f"{previous.com_port} para {current.com_port}."
            )
            await self._publish(EventType.USB_DEVICE_COM_CHANGED, current,
                                previous_com=previous.com_port, current_com=current.com_port)
            await self._history(EventType.USB_DEVICE_COM_CHANGED.value, current,
                                message, "NOTICE")
            await self._notify(current, message, level="NOTICE", kind="com_changed")
            self._logical_events += 1
            return
        fields = ("drive_letter", "product", "manufacturer", "device_class")
        changed = [field for field in fields if getattr(previous, field) != getattr(current, field)]
        if changed:
            await self._publish(EventType.USB_DEVICE_METADATA_CHANGED, current,
                                changed_fields=changed)
            await self._history(
                EventType.USB_DEVICE_METADATA_CHANGED.value, current,
                f"Metadados de {self._display_name(current)} atualizados: {', '.join(changed)}.",
                "INFO",
            )
            self._logical_events += 1

    async def _disconnected_event(self, record: UsbDeviceRecord) -> None:
        message = f"{self._display_name(record)} desconectado."
        await self._publish(EventType.USB_DEVICE_DISCONNECTED, record)
        await self._history(EventType.USB_DEVICE_DISCONNECTED.value, record, message, "INFO")
        await self._notify(record, message, level="INFO", kind="disconnected")
        self._logical_events += 1

    async def _publish(self, event_type: EventType, record: UsbDeviceRecord,
                       **extra: Any) -> None:
        await self.event_bus.publish(event_type, device=record.public_dict(), **extra)
        self.last_event = {
            "timestamp": utc_now(),
            "event_type": event_type.value,
            "device_id": record.device_id,
            "description": self._display_name(record),
        }

    async def _history(self, event_type: str, record: UsbDeviceRecord,
                       description: str, level: str) -> UsbHistoryEvent:
        event = await self.registry.append_history(UsbHistoryEvent(
            timestamp=utc_now(), event_type=event_type, device_id=record.device_id,
            name=record.name, friendly_name=record.friendly_name,
            vid=record.vid, pid=record.pid, com_port=record.com_port,
            drive_letter=record.drive_letter, known=record.registered,
            level=level, description=description,
        ))
        self.last_event = event.public_dict()
        return event

    async def _notify(self, record: UsbDeviceRecord, message: str, *, level: str,
                      kind: str, voice: bool = False) -> None:
        await self.event_bus.publish(
            EventType.MONITOR_NOTIFICATION,
            monitor_id=f"usb:{record.device_id}", objective="Monitoramento de dispositivos USB",
            message=message, kind=kind, severity=level.casefold(),
            status=self.state.value, voice=voice, source="usb_monitor",
            device_id=record.device_id,
        )

    async def _monitor_failure(self, error: str) -> None:
        await self.event_bus.publish(EventType.USB_MONITOR_FAILURE, error=error,
                                     state=UsbMonitorState.DEGRADED.value)
        await self.event_bus.publish(
            EventType.MONITOR_NOTIFICATION,
            monitor_id="usb:monitor", objective="Monitoramento de dispositivos USB",
            message=f"O monitor USB entrou em modo degradado: {error}",
            kind="monitor_failure", severity="warning", status="DEGRADED",
            voice=True, source="usb_monitor",
        )

    async def _maybe_duplicate_signal(self) -> None:
        if self._event_hints < 10:
            return
        rate = max(0.0, 1.0 - (self._logical_events / max(1, self._event_hints)))
        now = time.monotonic()
        if rate >= 0.5 and now - self._duplicate_signal_at >= 60:
            self._duplicate_signal_at = now
            await self.event_bus.publish(
                EventType.USB_EVENT_DUPLICATE_RATE,
                duplicate_rate=round(rate, 3), hints=self._event_hints,
                logical_events=self._logical_events,
            )

    async def _sync_computer_state(self) -> None:
        if self.computer_state is None:
            return
        connected = [record for record in self._connected.values()
                     if record.relevance == DeviceRelevance.USER_RELEVANT]
        compact = {
            "monitor_state": self.state.value,
            "connected_count": len(connected),
            "unknown_count": sum(not record.registered for record in connected),
            "connected_devices": [
                {
                    "device_id": record.device_id,
                    "name": self._display_name(record),
                    "category": record.category,
                    "com": record.com_port,
                    "drive": record.drive_letter,
                    "known": record.registered,
                    "trusted": record.trusted,
                }
                for record in connected[:12]
            ],
            "last_event": self.last_event,
        }
        self.computer_state.update(
            "usb", compact, source="windows_pnp", ttl_seconds=45,
            stale_after_seconds=max(90, self.reconciliation_interval_seconds * 3),
        )

    async def status_snapshot(self) -> dict[str, Any]:
        connected = [record for record in self._connected.values()
                     if record.relevance == DeviceRelevance.USER_RELEVANT]
        known = await self.registry.list_devices(registered=True)
        return {
            "monitor_state": self.state.value,
            "event_source": self.event_source if self._native_active else self.fallback,
            "fallback": self.fallback,
            "connected_count": len(connected),
            "known_count": len(known),
            "unknown_count": sum(not record.registered for record in connected),
            "system_internal_count": sum(
                record.relevance == DeviceRelevance.SYSTEM_INTERNAL
                for record in self._connected.values()
            ),
            "last_event": self.last_event,
            "last_heartbeat_at": self.last_heartbeat_at,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "dedup": {
                "native_hints": self._event_hints,
                "logical_events": self._logical_events,
            },
        }

    async def devices(self, *, include_internal: bool = False) -> list[dict[str, Any]]:
        records = await self.registry.list_devices(include_internal=include_internal)
        return [record.public_dict() for record in records]

    async def connected(self, *, include_internal: bool = False) -> list[dict[str, Any]]:
        records = [
            record for record in self._connected.values()
            if include_internal or record.relevance == DeviceRelevance.USER_RELEVANT
        ]
        records.sort(key=lambda record: (
            not record.registered, self._display_name(record).casefold()
        ))
        return [record.public_dict() for record in records]

    async def known(self) -> list[dict[str, Any]]:
        records = await self.registry.list_devices(registered=True)
        return [record.public_dict() for record in records]

    async def history(self, *, limit: int = 200,
                      event_type: str | None = None) -> list[dict[str, Any]]:
        events = await self.registry.history(limit=limit, event_type=event_type)
        return [event.public_dict() for event in events]

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        record = await self.registry.get(device_id)
        if record:
            self._last_relevant_device_id = record.device_id
        return record.public_dict() if record else None

    async def update_device(self, device_id: str, values: dict[str, Any]) -> dict[str, Any]:
        record, newly_registered = await self.registry.update(device_id, values)
        if device_id in self._connected:
            self._connected[device_id] = record
        self._last_relevant_device_id = device_id
        event_type = (EventType.USB_DEVICE_REGISTERED if newly_registered
                      else EventType.USB_DEVICE_METADATA_CHANGED)
        message = (
            f"{self._display_name(record)} registrado como dispositivo conhecido."
            if newly_registered else f"Registro de {self._display_name(record)} atualizado."
        )
        await self._publish(event_type, record)
        await self._history(event_type.value, record, message, "INFO")
        await self._sync_computer_state()
        return record.public_dict()

    async def forget_device(self, device_id: str) -> dict[str, Any]:
        record = await self.registry.forget(device_id)
        if device_id in self._connected:
            self._connected[device_id] = record
        await self._publish(EventType.USB_DEVICE_FORGOTTEN, record)
        await self._history(EventType.USB_DEVICE_FORGOTTEN.value, record,
                            f"Registro de {record.name} esquecido.", "INFO")
        await self._sync_computer_state()
        return record.public_dict()

    # ----------------------------------------------------------- chat/intents

    @staticmethod
    def can_handle_chat(text: str) -> bool:
        from app.usb.hardware import hardware_request
        if hardware_request(text) is not None:
            return True
        plain = _plain(text)
        return bool(re.search(
            r"\b(usb|dispositivo|dispositivos|com\d*|porta com|pendrive|armazenamento)\b",
            plain,
        ) or re.search(
            r"\b(registra|registre|chama|chame|renomeia|renomeie|esquece|esqueca)\b.+\b(ele|esse|este)\b",
            plain,
        ))

    async def handle_chat(self, text: str) -> str | None:
        from app.usb.hardware import discover_hardware, hardware_request, presence_reply

        request = hardware_request(text)
        if request is not None:
            self.last_hardware_observation = await discover_hardware(self, request)
            return presence_reply(self.last_hardware_observation)
        plain = _plain(text)
        usb_context = bool(re.search(
            r"\b(usb|dispositivo|dispositivos|com\d*|porta com|pendrive|armazenamento)\b",
            plain,
        ))
        known_names = await self.registry.list_devices(include_internal=False)
        named = self._match_named(plain, known_names)
        contextual_mutation = bool(re.search(
            r"\b(registra|registre|chama|chame|renomeia|renomeie|esquece|esqueca|marca|marque)\b",
            plain,
        ) and re.search(r"\b(ele|esse|este|dispositivo)\b", plain))
        if not usb_context and named is None and not contextual_mutation:
            return None

        if re.search(r"\b(registra|registre|registrar)\b", plain):
            target = named or await self._context_device(known_names)
            if target is None:
                return "Não há um dispositivo USB recente que eu possa registrar."
            match = re.search(r"(?i)\bcomo\s+(.+?)[.!?]*$", text.strip())
            friendly_name = match.group(1).strip()[:120] if match else None
            if not friendly_name:
                return "Diga o nome amigável, por exemplo: registra esse dispositivo como Proxmark3."
            record = await self.update_device(target.device_id, {
                "registered": True, "friendly_name": friendly_name,
            })
            return f"Registrei o dispositivo como {record['friendly_name']}."

        rename = re.search(r"(?i)\b(?:chama|chame|renomeia|renomeie)\b.+?\b(?:de|para)\s+(.+?)[.!?]*$", text.strip())
        if rename:
            target = named or await self._context_device(known_names)
            if target is None:
                return "Não consegui resolver qual dispositivo USB deve ser renomeado."
            name = rename.group(1).strip()[:120]
            record = await self.update_device(target.device_id, {
                "registered": True, "friendly_name": name,
            })
            return f"Agora esse dispositivo se chama {record['friendly_name']}."

        if re.search(r"\b(marca|marque)\b.+\bconfiavel\b", plain):
            target = named or await self._context_device(known_names)
            if target is None:
                return "Não consegui resolver qual dispositivo USB deve ser marcado."
            record = await self.update_device(target.device_id, {
                "registered": True, "trusted": True,
            })
            return (
                f"Marquei {self._display_name_dict(record)} como confiável. "
                "Isso significa apenas que ele foi reconhecido por você; não é uma garantia de segurança."
            )

        if re.search(r"\b(esquece|esqueca|esquecer)\b", plain):
            target = named or await self._context_device(known_names)
            if target is None:
                return "Não consegui resolver qual registro USB deve ser esquecido."
            old_name = self._display_name(target)
            await self.forget_device(target.device_id)
            return f"Esqueci o registro de {old_name}. Se reaparecer, será tratado como desconhecido."

        if "desconhecid" in plain:
            records = [record for record in known_names
                       if record.status == "CONNECTED" and not record.registered]
            return self._list_reply(records, "Não há USB desconhecido conectado.",
                                    "USB desconhecidos conectados")

        if any(token in plain for token in ("ja registrei", "registrados", "conhecidos")):
            records = [record for record in known_names if record.registered]
            return self._list_reply(records, "Nenhum dispositivo USB foi registrado ainda.",
                                    "Dispositivos USB conhecidos")

        target = named
        if target is None and any(token in plain for token in ("esse", "este", "ele", "dispositivo x")):
            target = await self._context_device(known_names)
        if target is not None and ("com" in plain or "porta" in plain):
            self._last_relevant_device_id = target.device_id
            if target.com_port:
                return f"{self._display_name(target)} está na {target.com_port}."
            return f"{self._display_name(target)} não possui porta COM detectada agora."
        if target is not None and any(token in plain for token in ("ultima vez", "quando")):
            self._last_relevant_device_id = target.device_id
            return f"A última conexão de {self._display_name(target)} foi em {target.last_connection}."
        if target is not None and "conectad" in plain:
            self._last_relevant_device_id = target.device_id
            return (f"Sim, {self._display_name(target)} está conectado"
                    + (f" na {target.com_port}." if target.com_port else ".")) \
                if target.status == "CONNECTED" else \
                f"Não. {self._display_name(target)} não está conectado agora."
        if target is not None:
            self._last_relevant_device_id = target.device_id
            return self._device_reply(target)

        if any(token in plain for token in ("quais", "lista", "listar", "estao conectados", "tem algum")):
            records = [record for record in known_names if record.status == "CONNECTED"]
            return self._list_reply(records, "Nenhum dispositivo USB relevante está conectado.",
                                    "USB conectados")
        return None

    async def _context_device(self, records: list[UsbDeviceRecord]) -> UsbDeviceRecord | None:
        if self._last_relevant_device_id:
            match = next((record for record in records
                          if record.device_id == self._last_relevant_device_id), None)
            if match:
                return match
            return await self.registry.get(self._last_relevant_device_id)
        connected_unknown = [record for record in records
                             if record.status == "CONNECTED" and not record.registered]
        if len(connected_unknown) == 1:
            return connected_unknown[0]
        return None

    @staticmethod
    def _match_named(plain_text: str,
                     records: list[UsbDeviceRecord]) -> UsbDeviceRecord | None:
        candidates: list[tuple[int, UsbDeviceRecord]] = []
        for record in records:
            names = (record.friendly_name, record.name, record.product)
            for name in names:
                normalized = _plain(str(name or ""))
                if len(normalized) >= 3 and normalized in plain_text:
                    candidates.append((len(normalized), record))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _list_reply(self, records: list[UsbDeviceRecord], empty: str, title: str) -> str:
        if not records:
            return empty
        parts = []
        for record in records[:12]:
            suffix = record.com_port or record.drive_letter
            parts.append(self._display_name(record) + (f" ({suffix})" if suffix else ""))
            self._last_relevant_device_id = record.device_id
        return f"{title}: " + "; ".join(parts) + "."

    def _device_reply(self, record: UsbDeviceRecord) -> str:
        pieces = [self._display_name(record), record.status.casefold()]
        if record.vid and record.pid:
            pieces.append(f"VID:PID {record.vid}:{record.pid}")
        if record.com_port:
            pieces.append(record.com_port)
        if record.drive_letter:
            pieces.append(f"unidade {record.drive_letter}")
        pieces.append("conhecido" if record.registered else "ainda não registrado")
        return ", ".join(pieces) + "."

    @staticmethod
    def _display_name(record: UsbDeviceRecord) -> str:
        return record.friendly_name or record.name

    @staticmethod
    def _display_name_dict(record: dict[str, Any]) -> str:
        return str(record.get("friendly_name") or record.get("name") or "dispositivo USB")

    def _known_connected_message(self, record: UsbDeviceRecord) -> str:
        suffix = f" na {record.com_port}" if record.com_port else (
            f" — unidade {record.drive_letter}" if record.drive_letter else ""
        )
        return f"{self._display_name(record)} conectado{suffix}."

    @staticmethod
    def _unknown_message(record: UsbDeviceRecord) -> str:
        name = record.product or record.name
        vid_pid = f" VID:PID {record.vid}:{record.pid}." if record.vid and record.pid else ""
        if record.category == "Armazenamento":
            drive = f" Unidade {record.drive_letter}." if record.drive_letter else ""
            return f"Novo armazenamento USB detectado: {name}.{drive} Ainda não registrado."
        return f"Novo dispositivo USB detectado: {name}.{vid_pid} Ainda não registrado."
