from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class UsbMonitorState(StrEnum):
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


class IdentityConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class DeviceRelevance(StrEnum):
    USER_RELEVANT = "USER_RELEVANT"
    SYSTEM_INTERNAL = "SYSTEM_INTERNAL"


class UsbDeviceObservation(BaseModel):
    device_id: str = ""
    identity_basis: str = ""
    identity_confidence: IdentityConfidence = IdentityConfidence.LOW
    name: str
    friendly_name: str | None = None
    category: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    vid: str | None = None
    pid: str | None = None
    serial: str | None = None
    device_instance_id: str | None = None
    container_id: str | None = None
    device_class: str | None = None
    class_guid: str | None = None
    parent_instance_id: str | None = None
    com_port: str | None = None
    drive_letter: str | None = None
    volume_label: str | None = None
    filesystem: str | None = None
    size_bytes: int | None = None
    interface_name: str | None = None
    network_state: str | None = None
    status: str = "CONNECTED"
    relevance: DeviceRelevance = DeviceRelevance.USER_RELEVANT
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def vid_pid(self) -> str | None:
        return f"{self.vid}:{self.pid}" if self.vid and self.pid else None


class UsbDeviceRecord(UsbDeviceObservation):
    registered: bool = False
    trusted: bool = False
    note: str | None = None
    first_seen: str
    last_seen: str
    last_connection: str
    last_disconnection: str | None = None
    present_at_startup: bool = False
    identity_changed: bool = False

    def public_dict(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["known"] = self.registered
        value["vid_pid"] = self.vid_pid
        return value


class UsbHistoryEvent(BaseModel):
    event_id: int | None = None
    timestamp: str
    event_type: str
    device_id: str
    name: str
    friendly_name: str | None = None
    vid: str | None = None
    pid: str | None = None
    com_port: str | None = None
    drive_letter: str | None = None
    known: bool = False
    level: str = "INFO"
    description: str

    def public_dict(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["vid_pid"] = f"{self.vid}:{self.pid}" if self.vid and self.pid else None
        return value


_VID_PID_RE = re.compile(r"VID[_:-]?([0-9A-F]{4}).*?PID[_:-]?([0-9A-F]{4})", re.I)
_SERIAL_LOCATION_RE = re.compile(r"^\d+&[0-9A-F]+&\d+&", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_vid_pid(*values: str | None) -> tuple[str | None, str | None]:
    """Extract and normalize VID/PID without assuming every PnP node has them."""
    for value in values:
        match = _VID_PID_RE.search(str(value or ""))
        if match:
            return match.group(1).upper(), match.group(2).upper()
    return None, None


def serial_from_instance_id(instance_id: str | None) -> str | None:
    """Return only serial-looking tails; location-derived ids are not serials."""
    parts = str(instance_id or "").split("\\")
    if len(parts) < 3:
        return None
    tail = parts[-1].strip().split("&0", 1)[0]
    if not tail or _SERIAL_LOCATION_RE.match(tail) or tail.startswith("{"):
        return None
    if "&" in tail and not str(instance_id).upper().startswith("USBSTOR\\"):
        return None
    return tail.upper()


def _normalized(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def fingerprint_device(observation: UsbDeviceObservation) -> tuple[str, str, IdentityConfidence]:
    """Use the strongest Windows identity available, never VID/PID alone."""
    vid_pid = f"{observation.vid or '-'}:{observation.pid or '-'}"
    if observation.serial:
        source, basis, confidence = (
            f"serial|{vid_pid}|{_normalized(observation.serial)}",
            "USB_SERIAL",
            IdentityConfidence.HIGH,
        )
    elif observation.container_id:
        source, basis, confidence = (
            f"container|{_normalized(observation.container_id)}",
            "CONTAINER_ID",
            IdentityConfidence.HIGH,
        )
    elif observation.device_instance_id:
        source, basis, confidence = (
            f"instance|{_normalized(observation.device_instance_id)}",
            "DEVICE_INSTANCE_ID",
            IdentityConfidence.MEDIUM,
        )
    elif observation.vid and observation.pid and (
        observation.manufacturer or observation.product
    ):
        source, basis, confidence = (
            "composite|" + "|".join((
                vid_pid,
                _normalized(observation.manufacturer),
                _normalized(observation.product or observation.name),
            )),
            "VID_PID_MANUFACTURER_PRODUCT",
            IdentityConfidence.LOW,
        )
    else:
        source, basis, confidence = (
            "fallback|" + "|".join((
                _normalized(observation.name),
                _normalized(observation.device_class),
                _normalized(observation.drive_letter),
            )),
            "CONTROLLED_FALLBACK",
            IdentityConfidence.LOW,
        )
    digest = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:32]
    return f"usb_{digest}", basis, confidence


def apply_fingerprint(observation: UsbDeviceObservation) -> UsbDeviceObservation:
    device_id, basis, confidence = fingerprint_device(observation)
    return observation.model_copy(update={
        "device_id": device_id,
        "identity_basis": basis,
        "identity_confidence": confidence,
    })
