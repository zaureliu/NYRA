from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import re
import sys
import uuid
from collections import defaultdict
from typing import Callable

from app.usb.models import (
    DeviceRelevance,
    UsbDeviceObservation,
    apply_fingerprint,
    parse_vid_pid,
    serial_from_instance_id,
)

logger = logging.getLogger("nyra.usb.discovery")

_IS_WINDOWS = sys.platform == "win32"
_NO_ERROR = 0
_ERROR_NO_MORE_ITEMS = 259
_DIGCF_PRESENT = 0x00000002
_DIGCF_ALLCLASSES = 0x00000004
_SPDRP_DEVICEDESC = 0x00000000
_SPDRP_HARDWAREID = 0x00000001
_SPDRP_CLASS = 0x00000007
_SPDRP_CLASSGUID = 0x00000008
_SPDRP_MFG = 0x0000000B
_SPDRP_FRIENDLYNAME = 0x0000000C
_SPDRP_LOCATION_INFORMATION = 0x0000000D
_SPDRP_ENUMERATOR_NAME = 0x00000016
_CM_NOTIFY_FILTER_FLAG_ALL_INTERFACE_CLASSES = 0x00000001
_CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE = 0
_MAX_DEVICE_ID_LEN = 200


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_text(cls, value: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(value.strip("{}")).bytes_le)

    def text(self) -> str:
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class _CM_NOTIFY_DEVICE_INTERFACE(ctypes.Structure):
    _fields_ = [("ClassGuid", GUID)]


class _CM_NOTIFY_DEVICE_HANDLE(ctypes.Structure):
    _fields_ = [("hTarget", wintypes.HANDLE)]


class _CM_NOTIFY_DEVICE_INSTANCE(ctypes.Structure):
    _fields_ = [("InstanceId", wintypes.WCHAR * _MAX_DEVICE_ID_LEN)]


class _CM_NOTIFY_UNION(ctypes.Union):
    _fields_ = [
        ("DeviceInterface", _CM_NOTIFY_DEVICE_INTERFACE),
        ("DeviceHandle", _CM_NOTIFY_DEVICE_HANDLE),
        ("DeviceInstance", _CM_NOTIFY_DEVICE_INSTANCE),
    ]


class CM_NOTIFY_FILTER(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("FilterType", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
        ("u", _CM_NOTIFY_UNION),
    ]


_CONTAINER_KEY = DEVPROPKEY(
    GUID.from_text("8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C"), 2
)
_BUS_DESCRIPTION_KEY = DEVPROPKEY(
    GUID.from_text("540B947E-8B40-45BC-A8A2-6A0B894CBDA2"), 4
)


def _clean_resource_text(value: str | None) -> str | None:
    text = str(value or "").strip().strip("\x00")
    if not text:
        return None
    if ";" in text and text.startswith("@"):
        text = text.rsplit(";", 1)[-1]
    return text.strip() or None


def _category(name: str, device_class: str, instance_id: str) -> str:
    text = f"{name} {device_class} {instance_id}".casefold()
    if "usbstor" in text or any(token in text for token in ("diskdrive", "mass storage", "volume")):
        return "Armazenamento"
    if (device_class.casefold() == "ports"
            or re.search(r"\bcom\d+\b", text)
            or any(token in text for token in ("serial", "uart", "ftdi", "cp210", "ch340"))):
        return "Serial"
    if any(token in text for token in ("audio", "microphone", "headset", "sound")):
        return "Áudio"
    if any(token in text for token in ("camera", "webcam", "image", "capture", "video")):
        return "Vídeo"
    if any(token in text for token in ("network", "net", "ethernet", "wi-fi", "wifi")):
        return "Rede"
    if any(token in text for token in ("keyboard", "mouse", "hidclass", "game controller")):
        return "HID"
    if any(token in text for token in ("wpd", "portable", "android", "iphone", "smartphone")):
        return "Smartphone"
    if "hub" in text:
        return "Hub"
    if any(token in text for token in (
        "proxmark", "chameleon", "esp32", "lilygo", "cardputer", "arduino",
        "rp2040", "stm32", "usb-uart",
    )):
        return "Hardware Lab"
    return "Outro"


def classify_relevance(name: str, instance_id: str) -> DeviceRelevance:
    text = f"{name} {instance_id}".casefold()
    if any(token in text for token in (
        "root hub", "root_hub", "host controller", "usb4 host router",
        "composite bus enumerator", "parsec virtual", "virtual mouse", "virtual keyboard",
    )):
        return DeviceRelevance.SYSTEM_INTERNAL
    return DeviceRelevance.USER_RELEVANT


class WindowsUsbDiscovery:
    """Enumerate USB/PnP metadata through SetupAPI; never reads device content."""

    def __init__(self) -> None:
        self.last_error: str | None = None

    def enumerate(self) -> list[UsbDeviceObservation]:
        if not _IS_WINDOWS:
            self.last_error = "WINDOWS_ONLY"
            return []
        try:
            observations = self._enumerate_setupapi()
            self.last_error = None
            return observations
        except Exception as error:  # noqa: BLE001 - monitor degrades instead of crashing startup
            self.last_error = f"{type(error).__name__}: {error}"[:240]
            logger.warning("usb_setupapi_enumeration_failed", extra={"error_type": type(error).__name__})
            return []

    def _enumerate_setupapi(self) -> list[UsbDeviceObservation]:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
        ]
        setupapi.SetupDiEnumDeviceInfo.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA),
        ]
        setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        setupapi.SetupDiGetDevicePropertyW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(DEVPROPKEY),
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
        ]
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        cfgmgr32.CM_Get_Parent.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG,
        ]
        cfgmgr32.CM_Get_Device_IDW.argtypes = [
            wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG,
        ]

        handle = setupapi.SetupDiGetClassDevsW(
            None, None, None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        nodes: dict[str, tuple[SP_DEVINFO_DATA, str | None]] = {}
        try:
            index = 0
            while True:
                info = SP_DEVINFO_DATA()
                info.cbSize = ctypes.sizeof(info)
                if not setupapi.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                    error = ctypes.get_last_error()
                    if error == _ERROR_NO_MORE_ITEMS:
                        break
                    raise ctypes.WinError(error)
                index += 1
                instance = ctypes.create_unicode_buffer(2048)
                if not setupapi.SetupDiGetDeviceInstanceIdW(
                    handle, ctypes.byref(info), instance, len(instance), None
                ):
                    continue
                instance_id = instance.value.upper()
                parent_id: str | None = None
                parent = wintypes.DWORD()
                if cfgmgr32.CM_Get_Parent(ctypes.byref(parent), info.DevInst, 0) == _NO_ERROR:
                    parent_buffer = ctypes.create_unicode_buffer(_MAX_DEVICE_ID_LEN)
                    if cfgmgr32.CM_Get_Device_IDW(parent, parent_buffer, len(parent_buffer), 0) == _NO_ERROR:
                        parent_id = parent_buffer.value.upper() or None
                nodes[instance_id] = (info, parent_id)

            relevant_cache: dict[str, bool] = {}

            def is_relevant(instance_id: str, trail: set[str] | None = None) -> bool:
                if instance_id in relevant_cache:
                    return relevant_cache[instance_id]
                upper = instance_id.upper()
                if upper.startswith(("USB\\", "USBSTOR\\")) or (
                    upper.startswith("HID\\") and "VID_" in upper
                ):
                    relevant_cache[instance_id] = True
                    return True
                trail = set() if trail is None else trail
                if instance_id in trail:
                    return False
                trail.add(instance_id)
                parent_id = nodes.get(instance_id, (None, None))[1]
                result = bool(parent_id and parent_id in nodes and is_relevant(parent_id, trail))
                relevant_cache[instance_id] = result
                return result

            raw: list[UsbDeviceObservation] = []
            for instance_id, (info, parent_id) in nodes.items():
                if not is_relevant(instance_id):
                    continue
                get_reg = lambda prop: self._registry_property(setupapi, handle, info, prop)
                description = _clean_resource_text(get_reg(_SPDRP_DEVICEDESC))
                friendly = _clean_resource_text(get_reg(_SPDRP_FRIENDLYNAME))
                manufacturer = _clean_resource_text(get_reg(_SPDRP_MFG))
                device_class = _clean_resource_text(get_reg(_SPDRP_CLASS))
                class_guid = _clean_resource_text(get_reg(_SPDRP_CLASSGUID))
                hardware_ids = get_reg(_SPDRP_HARDWAREID)
                enumerator = _clean_resource_text(get_reg(_SPDRP_ENUMERATOR_NAME))
                location = _clean_resource_text(get_reg(_SPDRP_LOCATION_INFORMATION))
                bus_description = self._device_property_text(
                    setupapi, handle, info, _BUS_DESCRIPTION_KEY
                )
                container_id = self._device_property_guid(
                    setupapi, handle, info, _CONTAINER_KEY
                )
                reg_values = self._instance_registry_values(instance_id)
                container_id = container_id or reg_values.get("container_id")
                com_port = reg_values.get("com_port")
                name = bus_description or friendly or description or "Dispositivo USB"
                product = bus_description or description or friendly
                vid, pid = parse_vid_pid(instance_id, hardware_ids)
                serial = serial_from_instance_id(instance_id)
                category = _category(name, device_class or "", instance_id)
                raw.append(UsbDeviceObservation(
                    name=name,
                    category=category,
                    manufacturer=manufacturer,
                    product=product,
                    vid=vid,
                    pid=pid,
                    serial=serial,
                    device_instance_id=instance_id,
                    container_id=container_id,
                    device_class=device_class,
                    class_guid=class_guid,
                    parent_instance_id=parent_id,
                    com_port=com_port,
                    interface_name=friendly if category == "Rede" else None,
                    network_state="CONNECTED" if category == "Rede" else None,
                    relevance=classify_relevance(name, instance_id),
                    metadata={
                        key: value for key, value in {
                            "enumerator": enumerator,
                            "location": location,
                            "hardware_ids": hardware_ids,
                        }.items() if value
                    },
                ))
            return self._merge_physical_devices(raw)
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(handle)

    @staticmethod
    def _registry_property(setupapi, handle, info: SP_DEVINFO_DATA, prop: int) -> str | None:
        buffer = ctypes.create_unicode_buffer(4096)
        data_type = wintypes.DWORD()
        needed = wintypes.DWORD()
        ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
            handle, ctypes.byref(info), prop, ctypes.byref(data_type),
            ctypes.cast(buffer, ctypes.c_void_p), ctypes.sizeof(buffer), ctypes.byref(needed),
        )
        if not ok:
            return None
        if prop == _SPDRP_HARDWAREID:
            length = min(len(buffer), max(1, needed.value // ctypes.sizeof(wintypes.WCHAR)))
            values = [value for value in "".join(buffer[:length]).split("\x00") if value]
            return " | ".join(values) or None
        return buffer.value or None

    @staticmethod
    def _device_property_text(setupapi, handle, info: SP_DEVINFO_DATA,
                              key: DEVPROPKEY) -> str | None:
        buffer = ctypes.create_unicode_buffer(1024)
        prop_type = wintypes.DWORD()
        needed = wintypes.DWORD()
        ok = setupapi.SetupDiGetDevicePropertyW(
            handle, ctypes.byref(info), ctypes.byref(key), ctypes.byref(prop_type),
            ctypes.cast(buffer, ctypes.c_void_p), ctypes.sizeof(buffer),
            ctypes.byref(needed), 0,
        )
        return _clean_resource_text(buffer.value) if ok else None

    @staticmethod
    def _device_property_guid(setupapi, handle, info: SP_DEVINFO_DATA,
                              key: DEVPROPKEY) -> str | None:
        buffer = ctypes.create_string_buffer(64)
        prop_type = wintypes.DWORD()
        needed = wintypes.DWORD()
        ok = setupapi.SetupDiGetDevicePropertyW(
            handle, ctypes.byref(info), ctypes.byref(key), ctypes.byref(prop_type),
            ctypes.cast(buffer, ctypes.c_void_p), ctypes.sizeof(buffer),
            ctypes.byref(needed), 0,
        )
        if not ok or needed.value < ctypes.sizeof(GUID):
            return None
        return GUID.from_buffer_copy(buffer.raw[:ctypes.sizeof(GUID)]).text()

    @staticmethod
    def _instance_registry_values(instance_id: str) -> dict[str, str]:
        try:
            import winreg

            path = rf"SYSTEM\CurrentControlSet\Enum\{instance_id}"
            values: dict[str, str] = {}
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                try:
                    values["container_id"] = str(winreg.QueryValueEx(key, "ContainerID")[0])
                except OSError:
                    pass
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path + r"\Device Parameters") as key:
                    port = str(winreg.QueryValueEx(key, "PortName")[0]).upper()
                    if re.fullmatch(r"COM\d+", port):
                        values["com_port"] = port
            except OSError:
                pass
            return values
        except OSError:
            return {}

    def _merge_physical_devices(self, raw: list[UsbDeviceObservation]) -> list[UsbDeviceObservation]:
        groups: dict[str, list[UsbDeviceObservation]] = defaultdict(list)
        for item in raw:
            group_id = item.container_id or self._root_instance(item, raw)
            groups[group_id or item.device_instance_id or item.name].append(item)

        ranks = {
            "Armazenamento": 9, "Serial": 8, "Áudio": 7, "Vídeo": 6,
            "Rede": 5, "HID": 4, "Smartphone": 3, "Hub": 2,
            "Hardware Lab": 1, "Outro": 0,
        }
        merged: list[UsbDeviceObservation] = []
        for items in groups.values():
            preferred = max(items, key=lambda item: (
                ranks.get(item.category or "", 0),
                bool(item.com_port),
                bool(item.product),
                -len(item.device_instance_id or ""),
            ))
            direct_usb = next((item for item in items if
                               str(item.device_instance_id).startswith("USB\\VID_")), preferred)
            best_name = next((item.name for item in items if
                              item.name not in {"USB Composite Device", "Dispositivo USB"}), preferred.name)
            category = max((item.category or "Outro" for item in items), key=lambda x: ranks.get(x, 0))
            combined = preferred.model_copy(update={
                "name": best_name,
                "category": category,
                "manufacturer": preferred.manufacturer or direct_usb.manufacturer,
                "product": preferred.product or direct_usb.product,
                "vid": preferred.vid or direct_usb.vid,
                "pid": preferred.pid or direct_usb.pid,
                "serial": preferred.serial or direct_usb.serial,
                "device_instance_id": direct_usb.device_instance_id or preferred.device_instance_id,
                "container_id": preferred.container_id or direct_usb.container_id,
                "com_port": next((item.com_port for item in items if item.com_port), None),
                "interface_name": next((item.interface_name for item in items if item.interface_name), None),
                "relevance": (
                    DeviceRelevance.SYSTEM_INTERNAL
                    if (classify_relevance(best_name, direct_usb.device_instance_id or "")
                        == DeviceRelevance.SYSTEM_INTERNAL
                        or all(item.relevance == DeviceRelevance.SYSTEM_INTERNAL for item in items))
                    else DeviceRelevance.USER_RELEVANT
                ),
                "metadata": {
                    **preferred.metadata,
                    "pnp_nodes": len(items),
                    "classes": sorted({item.device_class for item in items if item.device_class}),
                },
            })
            merged.append(apply_fingerprint(combined))

        # SetupAPI provides the physical storage identity. Drive metadata is
        # associated only when unambiguous, avoiding a false device/drive match.
        storage = [item for item in merged if item.category == "Armazenamento"]
        volumes = self._removable_volumes()
        if len(storage) == 1 and len(volumes) == 1:
            target = storage[0]
            volume = volumes[0]
            merged[merged.index(target)] = target.model_copy(update={
                **volume,
                "metadata": {**target.metadata, **volume.get("metadata", {})},
            })

        # A weak composite can collide for two identical units. Keep both by
        # disambiguating with the Windows instance id instead of merging them.
        seen: dict[str, int] = {}
        result: list[UsbDeviceObservation] = []
        for item in merged:
            count = seen.get(item.device_id, 0)
            seen[item.device_id] = count + 1
            if count:
                suffix = uuid.uuid5(uuid.NAMESPACE_OID, item.device_instance_id or str(count)).hex[:8]
                item = item.model_copy(update={
                    "device_id": f"{item.device_id}_{suffix}",
                    "identity_basis": f"{item.identity_basis}_COLLISION",
                })
            result.append(item)
        return sorted(result, key=lambda item: (item.relevance.value, item.name.casefold()))

    @staticmethod
    def _root_instance(item: UsbDeviceObservation,
                       items: list[UsbDeviceObservation]) -> str | None:
        by_id = {entry.device_instance_id: entry for entry in items if entry.device_instance_id}
        current = item
        seen: set[str] = set()
        while current.parent_instance_id and current.parent_instance_id in by_id:
            if current.parent_instance_id in seen:
                break
            seen.add(current.parent_instance_id)
            current = by_id[current.parent_instance_id]
        return current.device_instance_id

    @staticmethod
    def _removable_volumes() -> list[dict]:
        if not _IS_WINDOWS:
            return []
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        length = kernel32.GetLogicalDriveStringsW(0, None)
        if not length:
            return []
        buffer = ctypes.create_unicode_buffer(length + 1)
        kernel32.GetLogicalDriveStringsW(len(buffer), buffer)
        drives = [value for value in "".join(buffer).split("\x00") if value]
        result: list[dict] = []
        for root in drives:
            if kernel32.GetDriveTypeW(root) != 2:  # DRIVE_REMOVABLE only
                continue
            label = ctypes.create_unicode_buffer(261)
            filesystem = ctypes.create_unicode_buffer(261)
            serial = wintypes.DWORD()
            if not kernel32.GetVolumeInformationW(
                root, label, len(label), ctypes.byref(serial), None, None,
                filesystem, len(filesystem),
            ):
                continue
            free = ctypes.c_ulonglong()
            total = ctypes.c_ulonglong()
            kernel32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
            result.append({
                "drive_letter": root.rstrip("\\"),
                "volume_label": label.value or None,
                "filesystem": filesystem.value or None,
                "size_bytes": int(total.value) if total.value else None,
                "metadata": {"volume_serial": f"{serial.value:08X}"},
            })
        return result


class WindowsDeviceNotificationSource:
    """Native ConfigMgr PnP callback. The callback only enqueues a hint."""

    def __init__(self) -> None:
        self._handle = ctypes.c_void_p()
        self._callback = None
        self.last_error: str | None = None

    def start(self, on_change: Callable[[], None]) -> bool:
        if not _IS_WINDOWS:
            self.last_error = "WINDOWS_ONLY"
            return False
        try:
            cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
            callback_type = ctypes.WINFUNCTYPE(
                wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            )

            def callback(_notification, _context, _action, _data, _size):
                try:
                    on_change()
                except Exception:  # noqa: BLE001 - callback must always return promptly
                    pass
                return _NO_ERROR

            self._callback = callback_type(callback)
            notify_filter = CM_NOTIFY_FILTER()
            notify_filter.cbSize = ctypes.sizeof(notify_filter)
            notify_filter.Flags = _CM_NOTIFY_FILTER_FLAG_ALL_INTERFACE_CLASSES
            notify_filter.FilterType = _CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
            result = cfgmgr32.CM_Register_Notification(
                ctypes.byref(notify_filter), None, self._callback,
                ctypes.byref(self._handle),
            )
            if result != _NO_ERROR:
                self.last_error = f"CM_Register_Notification={result}"
                self._callback = None
                return False
            self.last_error = None
            return True
        except Exception as error:  # noqa: BLE001
            self.last_error = f"{type(error).__name__}: {error}"[:240]
            self._callback = None
            return False

    def stop(self) -> None:
        if not _IS_WINDOWS or not self._handle.value:
            return
        try:
            cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
            cfgmgr32.CM_Unregister_Notification(self._handle)
        finally:
            self._handle = ctypes.c_void_p()
            self._callback = None
