"""Windows UI Automation layer over comtypes/UIAutomationCore.

Accessibility-first interaction (spec §44-§61): elements are located by
AutomationId/Name/ControlType/ClassName inside a TARGET window only (§220);
coordinates are a last-resort fallback executed at the element's own center
after a verified lookup (§46, §64). Every action attempts a post-state read so
the Agent can ground its report (§185-§186).

All COM work happens on worker threads initialized per call (asyncio.to_thread
at the controller boundary).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from typing import Any

logger = logging.getLogger("nyra.desktop.uia")

# UI Automation runs on background workers and does not require an STA message
# pump. Ask comtypes to use MTA if this module is the first COM consumer.
if "comtypes" not in sys.modules and not hasattr(sys, "coinit_flags"):
    sys.coinit_flags = 0

_MAX_NODES = 220

_CONTROL_TYPE_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50036: "Table",
}


class UiaError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_lock = threading.Lock()
_thread_state = threading.local()


def _client_module():
    import comtypes.client

    return comtypes.client.GetModule("UIAutomationCore.dll")


def _get_automation():
    _ensure_com()
    automation = getattr(_thread_state, "automation", None)
    if automation is not None:
        return automation
    with _lock:
        automation = getattr(_thread_state, "automation", None)
        if automation is None:
            import comtypes.client

            module = _client_module()
            automation = comtypes.client.CreateObject(
                module.CUIAutomation, interface=module.IUIAutomation, clsctx=comtypes.CLSCTX_INPROC_SERVER,
            )
            _thread_state.automation = automation
        return automation


def _ensure_com():
    if getattr(_thread_state, "com_initialized", False):
        return
    try:
        # comtypes initializes COM automatically on the thread that imports it
        # for the first time. Calling CoInitialize again in that case leaves an
        # unmatched apartment count; on ThreadPoolExecutor shutdown this used
        # to surface as RPC_E_DISCONNECTED (0x80010108).
        already_loaded = "comtypes" in sys.modules
        import comtypes

        if already_loaded:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        _thread_state.com_initialized = True
    except Exception:  # noqa: BLE001
        pass


def release_current_thread() -> None:
    """Release UIA proxies from the apartment that created them.

    COM pointers are apartment-bound. Releasing the former process-global
    singleton during interpreter teardown could run on a different thread and
    trigger ``RPC_E_DISCONNECTED``. The apartment itself intentionally lives
    for the lifetime of its dedicated worker: some UIA providers raise that
    native exception from ``CoUninitialize`` even after all pointers are
    dropped. Windows releases the apartment when the worker thread terminates.
    """
    automation = getattr(_thread_state, "automation", None)
    if automation is not None:
        try:
            del _thread_state.automation
        except AttributeError:
            pass
        del automation
        # comtypes may retain apartment-bound pointer cycles until cyclic GC.
        # Collect them here, before CoUninitialize and on their owning thread;
        # otherwise a later main-thread collection can raise RPC_E_DISCONNECTED.
        import gc

        gc.collect()


def close_current_thread() -> None:
    """Close the worker-owned MTA exactly once before its thread exits."""
    release_current_thread()
    if not getattr(_thread_state, "com_initialized", False):
        return
    try:
        import comtypes

        comtypes.CoUninitialize()
    finally:
        _thread_state.com_initialized = False


# --------------------------------------------------------------------- helpers

def _property_condition(property_id: int, value):
    automation = _get_automation()
    module = _client_module()
    return automation.CreatePropertyCondition(property_id, value)


def _element_from_handle(hwnd: int):
    automation = _get_automation()
    try:
        element = automation.ElementFromHandle(int(hwnd))
    except Exception as exc:  # noqa: BLE001
        raise UiaError("UI_ELEMENT_NOT_FOUND", f"Janela hwnd={hwnd} não expõe UI Automation.") from exc
    if element is None:
        raise UiaError("UI_ELEMENT_NOT_FOUND", f"Janela hwnd={hwnd} não expõe UI Automation.")
    return element


def _describe_element(element) -> dict:
    info: dict[str, Any] = {}
    try:
        info["name"] = (element.CurrentName or "")[:120]
    except Exception:  # noqa: BLE001
        info["name"] = ""
    try:
        info["automation_id"] = element.CurrentAutomationId or ""
    except Exception:  # noqa: BLE001
        info["automation_id"] = ""
    try:
        info["control_type"] = _CONTROL_TYPE_NAMES.get(element.CurrentControlType, str(element.CurrentControlType))
    except Exception:  # noqa: BLE001
        info["control_type"] = "Unknown"
    try:
        info["class_name"] = element.CurrentClassName or ""
    except Exception:  # noqa: BLE001
        info["class_name"] = ""
    try:
        info["enabled"] = bool(element.CurrentIsEnabled)
    except Exception:  # noqa: BLE001
        info["enabled"] = False
    try:
        rect = element.CurrentBoundingRectangle
        info["rect"] = {"x": rect.left, "y": rect.top, "width": rect.right - rect.left, "height": rect.bottom - rect.top}
    except Exception:  # noqa: BLE001
        info["rect"] = None
    try:
        info["value"] = _read_value(element)
    except Exception:  # noqa: BLE001
        info["value"] = None
    return info


def _read_value(element) -> str | None:
    module = _client_module()
    try:
        pattern = element.GetCurrentPattern(module.UIA_ValuePatternId)
        if pattern:
            value = pattern.QueryInterface(module.IUIAutomationValuePattern).CurrentValue
            text = "" if value is None else str(value)
            return text[:2000]
    except Exception:  # noqa: BLE001
        pass
    try:
        pattern = element.GetCurrentPattern(module.UIA_LegacyIAccessiblePatternId).QueryInterface(
            module.IUIAutomationLegacyIAccessiblePattern
        )
        raw = pattern.CurrentValue
        if raw and str(raw).strip() not in {"", "0"}:
            return str(raw)[:2000]
    except Exception:  # noqa: BLE001
        pass
    return None


def _walk(element, depth: int, max_depth: int, counter: list[int], out: list[dict]) -> None:
    if depth > max_depth or counter[0] >= _MAX_NODES:
        return
    info = _describe_element(element)
    if info["rect"] and info["rect"]["width"] > 0:
        counter[0] += 1
        out.append({"depth": depth, **info})
    child = _first_child(element)
    while child is not None and counter[0] < _MAX_NODES:
        _walk(child, depth + 1, max_depth, counter, out)
        child = _next_sibling(child)


def _first_child(element):
    try:
        return _get_automation().RawViewWalker.GetFirstChildElement(element)
    except Exception:  # noqa: BLE001 - E_POINTER/leaf nodes are normal
        return None


def _next_sibling(element):
    try:
        return _get_automation().RawViewWalker.GetNextSiblingElement(element)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ public API

def inspect_window(hwnd: int, max_depth: int = 5) -> dict:
    """Structured dump of the target window's accessibility tree."""
    _ensure_com()

    def work() -> dict:
        root = _element_from_handle(hwnd)
        nodes: list[dict] = []
        _walk(root, 0, max(1, min(max_depth, 8)), [0], nodes)
        return {"success": True, "hwnd": hwnd, "node_count": len(nodes), "elements": nodes}

    return work()


def _norm(value: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", value or "").casefold()


def _find_first_by_property(hwnd: int, property_id: int, value: str):
    """Provider-side lookup (FindFirst) — never hangs on heavy subtrees."""
    automation = _get_automation()
    module = _client_module()
    root = _element_from_handle(hwnd)
    try:
        condition = _property_condition(property_id, value)
        return automation.FindFirst(4, condition, root)  # TreeScope_Descendants
    except Exception:  # noqa: BLE001
        return None


def _find_all_by_control_type(hwnd: int, type_id: int):
    automation = _get_automation()
    module = _client_module()
    root = _element_from_handle(hwnd)
    try:
        condition = _property_condition(module.UIA_ControlTypePropertyId, type_id)
        found = automation.FindAll(4, condition, root)
        if not found:
            return []
        return [found.GetElement(i) for i in range(found.Length)]
    except Exception:  # noqa: BLE001
        return []


def _norm(value: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", value or "").casefold()


def find_button(hwnd: int, name: str) -> dict | None:
    """Fast button lookup: exact normalized name first, then contains."""
    _ensure_com()
    module = _client_module()
    needle = _norm(name)
    candidates: list[tuple[bool, dict]] = []
    for element in _find_all_by_control_type(hwnd, module.UIA_ButtonControlTypeId):
        info = _describe_element(element)
        clean = _norm(info["name"]).lstrip("&")
        target = needle.lstrip("&")
        if clean == target:
            return info
        if target and target in clean:
            candidates.append((False, info))
    return candidates[0][1] if candidates else None


def find_in_window(hwnd: int, *, name: str = "", automation_id: str = "", control_type: str = "", class_name: str = "", limit: int = 12) -> dict:
    """Locate elements inside one window by structured criteria (§49)."""
    _ensure_com()
    criteria = {
        "name": name.strip(), "automation_id": automation_id.strip(),
        "control_type": control_type.strip(), "class_name": class_name.strip(),
    }
    if not any(criteria.values()):
        raise UiaError("CRITERIA_REQUIRED", "Informe ao menos um critério: name, automation_id, control_type ou class_name.")

    def matches(info: dict) -> bool:
        if criteria["name"] and _norm(criteria["name"]) not in _norm(info["name"]):
            return False
        if criteria["automation_id"] and criteria["automation_id"].casefold() != info["automation_id"].casefold():
            return False
        if criteria["control_type"] and criteria["control_type"].casefold() != info["control_type"].casefold():
            return False
        if criteria["class_name"] and criteria["class_name"].casefold() != info["class_name"].casefold():
            return False
        return True

    def work() -> dict:
        root = _element_from_handle(hwnd)
        nodes: list[dict] = []
        _walk(root, 0, 8, [0], nodes)
        found = [node for node in nodes if matches(node)][: max(1, min(limit, 40))]
        return {
            "success": True, "hwnd": hwnd,
            "count": len(found), "elements": found,
            "scanned": len(nodes),
        }

    return work()


def _resolve_element(hwnd: int, *, name: str = "", automation_id: str = "", control_type: str = ""):
    # Fast provider-side paths first (common dialogs host heavy subtrees that
    # make raw walks hang).
    if automation_id and not name and not control_type:
        element = _find_first_by_property(hwnd, _UIA_AUTOMATION_ID, automation_id)
        if element is not None:
            return _describe_element(element)
    if name and not automation_id and not control_type:
        element = _find_first_by_property(hwnd, _UIA_NAME, name)
        if element is not None:
            return _describe_element(element)
    result = find_in_window(hwnd, name=name, automation_id=automation_id, control_type=control_type, limit=1)
    elements = result.get("elements") or []
    if not elements:
        raise UiaError("UI_ELEMENT_NOT_FOUND", "Nenhum elemento corresponde aos critérios informados nesta janela.")
    return elements[0]


_UIA_AUTOMATION_ID = 30011
_UIA_NAME = 30005


def _pattern(element, pattern_id: int, interface):
    module = _client_module()
    raw = element.GetCurrentPattern(pattern_id)
    if not raw:
        return None
    try:
        return raw.QueryInterface(interface)
    except Exception:  # noqa: BLE001
        return None


def _mouse_click(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

    def event(flags: int) -> _INPUT:
        item = _INPUT()
        item.type = 0
        item.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
        return item

    left_down, left_up = 0x0002, 0x0004
    arr = (event(left_down), event(left_up))
    user32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))


def click_element(hwnd: int, *, name: str = "", automation_id: str = "", control_type: str = "", allow_coordinate_fallback: bool = True) -> dict:
    """Invoke/click a control and report the method used (§50, §185)."""
    _ensure_com()
    module = _client_module()
    if name and not automation_id and not control_type:
        target = find_button(hwnd, name)
        if target is None:
            target = _resolve_element(hwnd, name=name, automation_id=automation_id, control_type=control_type)
    else:
        target = _resolve_element(hwnd, name=name, automation_id=automation_id, control_type=control_type)

    def work() -> dict:
        fresh = _locate_live_element(hwnd, target)
        if fresh is None:
            raise UiaError("UI_ELEMENT_NOT_FOUND", "Elemento alvo desapareceu antes do clique.")
        detail: dict[str, Any] = {"target": {key: target[key] for key in ("name", "automation_id", "control_type")}}
        invoke = _pattern(fresh, module.UIA_InvokePatternId, module.IUIAutomationInvokePattern)
        if invoke is not None:
            invoke.Invoke()
            detail["method"] = "InvokePattern"
            return {"success": True, **detail}
        if allow_coordinate_fallback and target.get("rect"):
            rect = target["rect"]
            x = rect["x"] + rect["width"] // 2
            y = rect["y"] + rect["height"] // 2
            _mouse_click(x, y)
            detail["method"] = "coordinate_fallback"
            detail["at"] = {"x": x, "y": y}
            return {"success": True, **detail}
        raise UiaError("UI_ACTION_FAILED", "Elemento não suporta Invoke e fallback de coordenadas está desabilitado.")

    return work()


def _locate_live_element(hwnd: int, info: dict):
    if info.get("automation_id"):
        element = _find_first_by_property(hwnd, _UIA_AUTOMATION_ID, info["automation_id"])
        if element is not None:
            return element
    if info.get("name"):
        element = _find_first_by_property(hwnd, _UIA_NAME, info["name"])
        if element is not None:
            return element
    automation = _get_automation()
    root = _element_from_handle(hwnd)
    stack = [root]
    visited = 0
    while stack and visited < 4000:
        element = stack.pop()
        visited += 1
        try:
            if (element.CurrentAutomationId or "") == info["automation_id"] and (element.CurrentName or "") == info["name"]:
                return element
        except Exception:  # noqa: BLE001
            continue
        child = _first_child(element)
        while child is not None:
            stack.append(child)
            child = _next_sibling(child)
    return None


def set_text(hwnd: int, value: str, *, name: str = "", automation_id: str = "", control_type: str = "") -> dict:
    """Fill an edit/value control and READ BACK the stored value (§53, §186)."""
    _ensure_com()
    module = _client_module()
    target = _resolve_element(hwnd, name=name, automation_id=automation_id, control_type=control_type or "edit")

    def work() -> dict:
        fresh = _locate_live_element(hwnd, target)
        if fresh is None:
            raise UiaError("UI_ELEMENT_NOT_FOUND", "Elemento alvo desapareceu antes da escrita.")
        value_pattern = _pattern(fresh, module.UIA_ValuePatternId, module.IUIAutomationValuePattern)
        if value_pattern is None:
            raise UiaError("UI_ACTION_FAILED", "Controle não suporta SetValue (ValuePattern ausente).")
        value_pattern.SetValue(value)
        time.sleep(0.15)
        stored = _read_value(fresh) or ""
        verified = stored == value
        return {
            "success": True, "effect_verified": verified,
            "verification_status": "VERIFIED" if verified else "VERIFICATION_FAILED",
            "stored_preview": stored[:120],
            "target": {key: target[key] for key in ("name", "automation_id", "control_type")},
        }

    return work()


def get_text(hwnd: int, *, name: str = "", automation_id: str = "", control_type: str = "") -> dict:
    _ensure_com()
    target = _resolve_element(hwnd, name=name, automation_id=automation_id, control_type=control_type)

    def work() -> dict:
        fresh = _locate_live_element(hwnd, target)
        if fresh is None:
            raise UiaError("UI_ELEMENT_NOT_FOUND", "Elemento alvo não está mais presente.")
        value = _read_value(fresh)
        info = _describe_element(fresh)
        return {"success": True, "value": value, "element": {key: info[key] for key in ("name", "automation_id", "control_type")}}

    return work()


def send_keys_to_foreground(text: str, expected_hwnd: int | None = None) -> dict:
    """Fallback input engine: type into the VERIFIED foreground window (§62, §65-66)."""
    _ensure_com()
    user32 = ctypes.windll.user32
    foreground = int(user32.GetForegroundWindow() or 0)
    if expected_hwnd and foreground != int(expected_hwnd):
        raise UiaError("FOCUS_NOT_CONFIRMED", "A janela alvo não está em primeiro plano; input de teclado abortado.")
    if not foreground:
        raise UiaError("FOCUS_NOT_CONFIRMED", "Nenhuma janela em primeiro plano para receber teclado.")

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short), ("wParamH", ctypes.c_ushort)]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

    key_up_flag = 0x0002
    inputs: list[_INPUT] = []
    hkl = user32.GetKeyboardLayout(0)

    _SPECIAL = {"enter": (0x0D, 0x1C), "tab": (0x09, 0x0F), "esc": (0x1B, 0x01),
                "escape": (0x1B, 0x01), "backspace": (0x08, 0x0E), "delete": (0x2E, 0x53)}
    _MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}

    def push(vk: int, scan: int, up: bool = False) -> None:
        item = _INPUT()
        item.type = 1
        flags = key_up_flag if up else 0
        if vk >= 0xE000:
            flags |= 0x0001
        item.ki = _KEYBDINPUT(vk & 0xFFFF, scan & 0xFFFF, flags, 0, None)
        inputs.append(item)

    def push_combo(combo: str) -> bool:
        """'{ctrl+s}', '{ctrl+shift+esc}' — pressiona modificadores, tecla, solta."""
        parts = [part.strip().casefold() for part in combo.strip("{}").split("+") if part.strip()]
        if not parts:
            return False
        mods = [name for name in parts[:-1] if name in _MODIFIERS]
        if len(mods) != len(parts) - 1:
            return False
        last = parts[-1]
        if len(last) == 1 and last.isalnum():
            vk = ord(last.upper())
            scan = user32.MapVirtualKeyW(vk, 0)
        elif last in _SPECIAL:
            vk, scan = _SPECIAL[last]
        else:
            return False
        for mod in mods:
            push(_MODIFIERS[mod], user32.MapVirtualKeyW(_MODIFIERS[mod], 0))
        push(vk, scan)
        push(vk, scan, up=True)
        for mod in reversed(mods):
            push(_MODIFIERS[mod], user32.MapVirtualKeyW(_MODIFIERS[mod], 0), up=True)
        return True

    index = 0
    # Tolerância: "{ctrl}s" → "{ctrl+s}" (mesma semântica, sintaxe comum).
    import re as _re

    text = _re.sub(
        r"\{(ctrl|alt|shift|win)\}(?=[A-Za-z0-9])",
        lambda match: "{" + match.group(1) + "+",
        text,
        flags=_re.IGNORECASE,
    )
    while index < len(text):
        if text[index] == "{":
            closing = text.find("}", index)
            if closing != -1 and push_combo(text[index:closing + 1]):
                index = closing + 1
                continue
        char = text[index]
        if char == "\n":
            push(0x0D, 0x1C)
            push(0x0D, 0x1C, up=True)
            index += 1
            continue
        if char == "\t":
            push(0x09, 0x0F)
            push(0x09, 0x0F, up=True)
            index += 1
            continue
        code = user32.VkKeyScanW(ord(char), hkl) & 0xFFFF
        vk = code & 0xFF
        shift = bool(code & 0x0100)
        scan = user32.MapVirtualKeyW(vk, 0)
        if shift:
            push(0x10, 0x2A)
        push(vk, scan)
        push(vk, scan, up=True)
        if shift:
            push(0x10, 0x2A, up=True)
        index += 1

    array = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise UiaError("UI_ACTION_FAILED", f"SendInput enviou {sent}/{len(inputs)} eventos.")
    return {"success": True, "chars_sent": len(inputs), "foreground_hwnd": foreground}
