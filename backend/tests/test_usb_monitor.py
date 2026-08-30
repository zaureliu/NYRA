from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import router
from app.computer.state import ComputerStateService
from app.events import EventBus, EventType
from app.usb.models import (
    IdentityConfidence,
    UsbDeviceObservation,
    apply_fingerprint,
    parse_vid_pid,
)
from app.usb.registry import UsbDeviceRegistry
from app.usb.service import UsbDeviceService


def device(
    *,
    name: str = "USB Serial Device",
    instance: str = r"USB\VID_1A86&PID_7523\CH340-TEST-01",
    container: str | None = "{11111111-2222-3333-4444-555555555555}",
    serial: str | None = "CH340-TEST-01",
    category: str = "Serial",
    com: str | None = "COM5",
    drive: str | None = None,
) -> UsbDeviceObservation:
    vid, pid = parse_vid_pid(instance)
    return apply_fingerprint(UsbDeviceObservation(
        name=name,
        category=category,
        manufacturer="NYRA Test Fixtures",
        product=name,
        vid=vid,
        pid=pid,
        serial=serial,
        device_instance_id=instance,
        container_id=container,
        device_class="Ports" if category == "Serial" else "DiskDrive",
        com_port=com,
        drive_letter=drive,
    ))


class FakeDiscovery:
    def __init__(self, rows: list[UsbDeviceObservation]) -> None:
        self.rows = rows
        self.last_error = None
        self.calls = 0

    def enumerate(self) -> list[UsbDeviceObservation]:
        self.calls += 1
        return [row.model_copy(deep=True) for row in self.rows]


class FakeNotificationSource:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.last_error = None if available else "FIXTURE_NATIVE_UNAVAILABLE"
        self.callback = None
        self.stopped = False

    def start(self, callback) -> bool:
        self.callback = callback
        return self.available

    def stop(self) -> None:
        self.stopped = True

    def emit(self, times: int = 1) -> None:
        assert self.callback is not None
        for _ in range(times):
            self.callback()


async def wait_for_event(bus: EventBus, event_type: EventType, *, count: int = 1) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while sum(event.type == event_type for event in bus.history()) < count:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"event not emitted: {event_type}")
        await asyncio.sleep(0.02)


def test_vid_pid_and_fingerprint_identity_priority() -> None:
    assert parse_vid_pid(r"USB\VID_2d2d&PID_504m") == (None, None)
    assert parse_vid_pid(r"USB\VID_2d2d&PID_504D\ABC") == ("2D2D", "504D")

    first = device(instance=r"USB\VID_2D2D&PID_504D\A", serial="PROXMARK-42")
    moved = device(instance=r"USB\VID_2D2D&PID_504D\B", serial="PROXMARK-42", com="COM9")
    other = device(instance=r"USB\VID_2D2D&PID_504D\C", serial="PROXMARK-43")
    assert first.device_id == moved.device_id
    assert first.device_id != other.device_id
    assert first.identity_confidence is IdentityConfidence.HIGH
    assert first.identity_basis == "USB_SERIAL"

    no_serial_a = device(serial=None, container="{AAAAAAAA-2222-3333-4444-555555555555}")
    no_serial_b = device(serial=None, container="{BBBBBBBB-2222-3333-4444-555555555555}")
    assert no_serial_a.device_id != no_serial_b.device_id


@pytest.mark.asyncio
async def test_registry_persists_known_name_trusted_history_and_forget(tmp_path) -> None:
    root = tmp_path / "usb-devices"
    registry = UsbDeviceRegistry(root)
    await registry.initialize()
    observation = device()
    record, previous = await registry.observe(observation, new_connection=True)
    assert previous is None
    assert not record.registered

    updated, newly_registered = await registry.update(record.device_id, {
        "registered": True,
        "friendly_name": "Proxmark3",
        "category": "Hardware Lab",
        "trusted": True,
        "note": "Ferramenta RFID/NFC do laboratório",
    })
    assert newly_registered
    assert updated.friendly_name == "Proxmark3"
    assert updated.trusted

    restarted = UsbDeviceRegistry(root)
    await restarted.initialize()
    recovered = await restarted.get(record.device_id)
    assert recovered is not None
    assert recovered.friendly_name == "Proxmark3"
    assert recovered.category == "Hardware Lab"
    assert recovered.trusted

    forgotten = await restarted.forget(record.device_id)
    assert not forgotten.registered
    assert not forgotten.trusted
    assert forgotten.friendly_name is None


@pytest.mark.asyncio
async def test_native_background_connect_disconnect_com_change_dedup_restart_and_context(tmp_path) -> None:
    registry_root = tmp_path / "usb-registry"
    initial = device()
    discovery = FakeDiscovery([initial])
    source = FakeNotificationSource()
    bus = EventBus(history_size=300)
    state = ComputerStateService(base_dir=tmp_path / "computer-state")
    service = UsbDeviceService(
        bus, computer_state=state,
        registry=UsbDeviceRegistry(registry_root),
        discovery=discovery, notification_source=source,
        debounce_seconds=0.05, reconciliation_interval_seconds=60,
    )
    await service.start()
    assert (await service.status_snapshot())["monitor_state"] == "ACTIVE"
    assert not [event for event in bus.history()
                if event.type == EventType.MONITOR_NOTIFICATION]
    usb_state, _freshness = state.get("usb")
    assert usb_state["connected_count"] == 1
    assert usb_state["unknown_count"] == 1

    reply = await service.handle_chat("registra ele como Proxmark3")
    assert reply == "Registrei o dispositivo como Proxmark3."
    trust_reply = await service.handle_chat("marca esse dispositivo como confiável")
    assert "reconhecido por você" in trust_reply
    assert await service.handle_chat("qual COM do Proxmark3?") == "Proxmark3 está na COM5."

    # Physical state changes in the fixture; native hints advance the service
    # without any new user/chat request. Eight duplicate PnP hints become one.
    discovery.rows = []
    source.emit(8)
    await wait_for_event(bus, EventType.USB_DEVICE_DISCONNECTED)
    assert discovery.calls == 2
    assert (await service.status_snapshot())["connected_count"] == 0

    discovery.rows = [initial.model_copy(update={"com_port": "COM7"})]
    source.emit(8)
    await wait_for_event(bus, EventType.USB_DEVICE_COM_CHANGED)
    await asyncio.sleep(0.1)  # COM event is published before its proactive chat notice.
    com_events = [event for event in bus.history()
                  if event.type == EventType.USB_DEVICE_COM_CHANGED]
    assert len(com_events) == 1
    assert com_events[0].payload["previous_com"] == "COM5"
    assert com_events[0].payload["current_com"] == "COM7"
    notifications = [event for event in bus.history()
                     if event.type == EventType.MONITOR_NOTIFICATION]
    assert sum("porta mudou" in str(event.payload.get("message"))
               for event in notifications) == 1
    await service.stop()
    assert source.stopped

    # Restart loads the same registry and baselines the already-present device
    # without a false connected notification.
    restart_bus = EventBus(history_size=100)
    restart_source = FakeNotificationSource()
    restarted = UsbDeviceService(
        restart_bus,
        registry=UsbDeviceRegistry(registry_root),
        discovery=FakeDiscovery(discovery.rows), notification_source=restart_source,
        debounce_seconds=0.05, reconciliation_interval_seconds=60,
    )
    await restarted.start()
    recovered = (await restarted.known())[0]
    assert recovered["friendly_name"] == "Proxmark3"
    assert recovered["trusted"] is True
    assert recovered["com_port"] == "COM7"
    assert not [event for event in restart_bus.history()
                if event.type == EventType.MONITOR_NOTIFICATION]
    await restarted.stop()


@pytest.mark.asyncio
async def test_unknown_storage_notification_api_and_natural_cancel_registration(tmp_path) -> None:
    baseline = device(name="Proxmark3", instance=r"USB\VID_2D2D&PID_504D\PROX-01",
                      serial="PROX-01", com="COM5")
    discovery = FakeDiscovery([baseline])
    source = FakeNotificationSource()
    bus = EventBus(history_size=200)
    service = UsbDeviceService(
        bus, registry=UsbDeviceRegistry(tmp_path / "registry"),
        discovery=discovery, notification_source=source,
        debounce_seconds=0.05, reconciliation_interval_seconds=60,
    )
    await service.start()
    storage = device(
        name="Kingston DataTraveler",
        instance=r"USB\VID_0951&PID_1666\KINGSTON-01",
        serial="KINGSTON-01", category="Armazenamento", com=None, drive="E:",
    )
    discovery.rows = [baseline, storage]
    source.emit()
    await wait_for_event(bus, EventType.USB_DEVICE_UNKNOWN)
    warning = [event for event in bus.history()
               if event.type == EventType.MONITOR_NOTIFICATION][-1]
    assert warning.payload["severity"] == "warning"
    assert warning.payload["kind"] == "unknown_storage"
    assert "Kingston DataTraveler" in warning.payload["message"]

    app = FastAPI()
    app.include_router(router)
    app.state.services = SimpleNamespace(usb=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get("/api/usb/status")
        connected = await client.get("/api/usb/devices/connected")
        history = await client.get("/api/usb/history")
        assert status.status_code == connected.status_code == history.status_code == 200
        assert status.json()["connected_count"] == 2
        assert len(connected.json()["devices"]) == 2
        device_id = storage.device_id
        updated = await client.put(f"/api/usb/devices/{device_id}", json={
            "registered": True, "friendly_name": "USB Teste",
            "category": "Hardware Lab", "trusted": True,
        })
        assert updated.status_code == 200
        assert updated.json()["friendly_name"] == "USB Teste"
        forgotten = await client.delete(f"/api/usb/devices/{device_id}")
        assert forgotten.status_code == 200
        assert forgotten.json()["known"] is False
    assert "Esqueci" in (await service.handle_chat("esquece esse dispositivo") or "")
    await service.stop()
