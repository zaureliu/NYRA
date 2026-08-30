from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.computer.intent import IntentUnderstandingService, NormalizedUserIntent, PlanStep
from app.computer.state import ComputerStateService
from app.desktop.canonical_apps import canonicalize_candidates
from app.desktop.compound import ActionContext, CompoundActionExecutor, parse_compound_intent
from app.desktop.discovery import ApplicationCandidate, LaunchMethod
from app.desktop.universal_registry import UniversalAppEntry, UniversalAppRegistry
from app.desktop.visual_fallback import locate_bottom_edit_surface, region_changed
from app.operator.vision_capture import Frame


def _candidate(app_id: str, name: str, method: str, target: str, source: str):
    return ApplicationCandidate(
        id=app_id,
        display_name=name,
        source=source,
        launch_method=method,
        target=target,
        confidence=0.9,
    )


class StaticDiscovery:
    enabled = True

    def __init__(self, values):
        self.values = list(values)

    def index(self, force=False):
        return list(self.values)

    def candidates_for(self, app_id):
        return [item for item in self.values if item.id == app_id]

    @staticmethod
    def revalidate(_candidate):
        return True


def test_notepad_discovery_sources_collapse_to_one_canonical_application(tmp_path):
    values = [
        _candidate("blocodenotas", "Bloco de Notas", LaunchMethod.START_MENU,
                   r"C:\Menu\Bloco de Notas.lnk", "start_menu"),
        _candidate("notepad", "notepad", LaunchMethod.EXE,
                   r"C:\Windows\System32\notepad.exe", "app_paths"),
        _candidate("blocodenotas", "Bloco de Notas", LaunchMethod.APP_USER_MODEL_ID,
                   "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App", "get_start_apps"),
        _candidate("notepad", "Notepad", LaunchMethod.EXE,
                   "notepad.exe", "path"),
    ]
    canonical = canonicalize_candidates(values)
    assert {item.id for item in canonical} == {"windows_notepad"}
    assert {item.display_name for item in canonical} == {"Bloco de Notas"}

    registry = UniversalAppRegistry(
        discovery=StaticDiscovery(values), root=tmp_path / "registry",
    )
    registry.refresh(force=True)
    assert list(registry.entries) == ["windows_notepad"]
    assert len(registry.entries["windows_notepad"].launch_options) == 4
    for alias in ("notepad", "notepad.exe", "bloco de notas"):
        resolution = registry.resolve_identity(alias)
        assert resolution["status"] == "EXACT_MATCH"
        assert resolution["entry"].app_id == "windows_notepad"


def test_shared_alias_between_distinct_apps_remains_true_ambiguity(tmp_path):
    registry = UniversalAppRegistry(
        discovery=StaticDiscovery([]), root=tmp_path / "registry",
    )
    registry.entries = {
        "studio_one": UniversalAppEntry(
            app_id="studio_one", display_name="Studio One", aliases=["studio"],
        ),
        "studio_two": UniversalAppEntry(
            app_id="studio_two", display_name="Studio Two", aliases=["studio"],
        ),
    }
    resolution = registry.resolve_identity("studio")
    assert resolution["status"] == "AMBIGUOUS"
    assert {item.app_id for item in resolution["entries"]} == {"studio_one", "studio_two"}


@pytest.mark.parametrize(
    ("utterance", "target", "final_action", "argument"),
    [
        ("abre o bloco de notas e escreva 'oi'", "bloco de notas", "type_text", "oi"),
        ("abre o notepad e escreva 'teste'", "notepad", "type_text", "teste"),
        ("abre o Discord e envie 'ola' no canal aberto", "discord", "send_text", "ola"),
        ("abre o Chrome e pesquise por Proxmox", "chrome", "search", "Proxmox"),
        ("abre o Canva e maximiza", "canva", "maximize", None),
        ("abre o Spotify e coloca ele em primeiro plano", "spotify", "focus", None),
        ("abre outro bloco de notas e escreva 'novo'", "bloco de notas", "type_text", "novo"),
    ],
)
def test_compound_parser_is_app_agnostic(utterance, target, final_action, argument):
    plan = parse_compound_intent(utterance)
    assert plan is not None
    assert plan.target == target
    assert plan.final_action == final_action
    assert plan.steps[0].capability == "open_or_focus"
    assert plan.steps[1].capability == "wait_for_ready"
    if argument is not None:
        assert argument in plan.steps[-1].arguments.values()
    if utterance.startswith("abre outro"):
        assert plan.explicit_new is True


def test_type_and_send_have_different_semantics():
    write = parse_compound_intent("abre o Discord e escreva 'ola' no canal aberto")
    send = parse_compound_intent("abre o Discord e envie 'ola' no canal aberto")
    assert write and send
    assert write.steps[-1].capability == "type_text"
    assert send.steps[-1].capability == "send_text"


def test_executable_alias_is_an_application_not_an_artifact():
    from app.desktop.intents import UniversalAction, parse_universal_intent

    parsed = parse_universal_intent("abre o notepad.exe")
    assert parsed is not None
    assert parsed.action == UniversalAction.OPEN_APP
    assert parsed.target == "notepad.exe"


def test_contextual_compound_intent_reuses_last_verified_target(tmp_path):
    state = ComputerStateService(base_dir=tmp_path)
    state.note_action(
        action="PLAN", kind="app", display_name="Bloco de Notas", verified=True,
        process_names=("notepad.exe",), title_tokens=("bloco de notas",), hwnd=55,
    )
    resolved = IntentUnderstandingService(state).resolve("escreva 'mais uma linha' nele")
    assert resolved is not None and resolved.action == "PLAN"
    assert resolved.target == "Bloco de Notas"
    assert resolved.resolved and resolved.resolved.hwnd == 55
    assert [step.capability for step in resolved.plan] == ["wait_for_ready", "type_text"]


class FakeController:
    def __init__(self, *, already_open=False):
        self.already_open = already_open
        self.launch_calls = []
        self.last_operation_result = None
        self.noted = None

    async def launch_dynamic(self, target, *, origin, force_new):
        self.launch_calls.append((target, origin, force_new))
        return {
            "success": True,
            "effect_verified": True,
            "app": "Bloco de Notas",
            "already_open": self.already_open,
            "candidate": {
                "id": "windows_notepad",
                "display_name": "Bloco de Notas",
                "process_names": ["notepad.exe"],
            },
            "windows": [{"hwnd": 55, "pid": 100}],
        }

    def _note_controlled(self, display_name, **kwargs):
        self.noted = (display_name, kwargs)


def _intent(*, force_new=False):
    return NormalizedUserIntent(
        intent_id="int_compound",
        turn_id="turn_compound",
        action="PLAN",
        target="notepad",
        arguments={"plan_kind": "compound_app", "final_action": "type_text", "text": "oi"},
        plan=[
            PlanStep(step=1, capability="open_or_focus", target="notepad",
                     arguments={"force_new": "true" if force_new else "false"}),
            PlanStep(step=2, capability="wait_for_ready", target="notepad"),
            PlanStep(step=3, capability="type_text", target="notepad",
                     arguments={"text": "oi"}),
        ],
    )


@pytest.mark.asyncio
async def test_sequential_executor_owns_target_and_bypasses_remote_and_agent(monkeypatch):
    controller = FakeController(already_open=True)
    executor = CompoundActionExecutor(controller)
    observed = {}

    async def ready(context, timeout=12.0):
        observed["ready_hwnd"] = context.hwnd
        return True

    async def typed(context, text, *, send):
        observed.update(hwnd=context.hwnd, text=text, send=send)
        return {"ok": True, "evidence": {"effect_verified": True}}

    monkeypatch.setattr(executor, "_wait_for_ready", ready)
    monkeypatch.setattr(executor, "_type_or_send", typed)
    result = await executor.execute(_intent(), turn_id="turn_compound")

    assert result["success"] is True
    assert result["user_facing_response"] == "Pronto. Escrevi oi no Bloco de Notas."
    assert result["remote_shell_calls"] == 0
    assert result["agent_run_calls"] == 0
    assert observed == {"ready_hwnd": 55, "hwnd": 55, "text": "oi", "send": False}
    assert controller.launch_calls == [("notepad", "compound_fastpath", False)]
    assert controller.noted[1]["canonical_id"] == "windows_notepad"
    for forbidden in ("PID", "HWND", "canonical_id", "JSON"):
        assert forbidden.casefold() not in result["user_facing_response"].casefold()


@pytest.mark.asyncio
async def test_explicit_new_instance_is_forwarded_and_plan_is_consumed_once(monkeypatch):
    controller = FakeController()
    executor = CompoundActionExecutor(controller)
    monkeypatch.setattr(executor, "_wait_for_ready", lambda context, timeout=12.0: _async(True))
    monkeypatch.setattr(executor, "_type_or_send",
                        lambda context, text, send: _async({"ok": True, "evidence": {}}))
    intent = _intent(force_new=True)
    first = await executor.execute(intent, turn_id="turn_compound")
    second = await executor.execute(intent, turn_id="turn_compound")
    assert first is second
    assert controller.launch_calls == [("notepad", "compound_fastpath", True)]


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_failed_required_step_never_reports_success(monkeypatch):
    controller = FakeController()
    executor = CompoundActionExecutor(controller)
    monkeypatch.setattr(executor, "_wait_for_ready", lambda context, timeout=12.0: _async(True))
    monkeypatch.setattr(executor, "_type_or_send",
                        lambda context, text, send: _async({"ok": False, "evidence": {}}))
    result = await executor.execute(_intent(), turn_id="failed_turn")
    assert result["success"] is False
    assert result["effect_verified"] is False
    assert "Escrevi" not in result["user_facing_response"]


def _frame(width=800, height=600, *, changed=False):
    pixels = bytearray([28, 28, 28, 255] * (width * height))
    for y in range(490, 555):
        for x in range(175, 780):
            offset = (y * width + x) * 4
            pixels[offset:offset + 4] = bytes((55, 55, 55, 255))
    if changed:
        for y in range(535, 550):
            for x in range(470, 520):
                offset = (y * width + x) * 4
                pixels[offset:offset + 4] = bytes((225, 225, 225, 255))
    return Frame(
        frame_id="test", timestamp=0, monitor_id=1, window_handle=10,
        width=width, height=height, pixels=bytes(pixels),
    )


def test_visual_fallback_derives_relative_surface_and_verifies_local_delta():
    before = _frame()
    after = _frame(changed=True)
    surface = locate_bottom_edit_surface(before)
    assert surface is not None
    assert 0.7 <= surface.y / before.height <= 0.95
    assert surface.width >= before.width * 0.7
    evidence = region_changed(before, after, surface)
    assert evidence["verified"] is True


def test_native_mouse_click_passes_ctypes_array_to_send_input(monkeypatch):
    import ctypes
    from app.desktop import uia

    observed = {}

    class FakeUser32:
        @staticmethod
        def SetCursorPos(_x, _y):
            return 1

        @staticmethod
        def SendInput(count, array, _size):
            observed["is_array"] = isinstance(array, ctypes.Array)
            return count

    monkeypatch.setattr(uia.ctypes, "windll", SimpleNamespace(user32=FakeUser32()))
    monkeypatch.setattr(uia.time, "sleep", lambda _seconds: None)
    uia._mouse_click(10, 20)
    assert observed["is_array"] is True


@pytest.mark.asyncio
async def test_send_submits_only_after_verified_typing(monkeypatch):
    from app.desktop import window_manager as wm

    calls = []

    class UiController:
        async def _uia_call(self, fn, *args, **kwargs):
            calls.append((fn.__name__, args, kwargs))
            if fn.__name__ == "set_text":
                return {"success": True, "effect_verified": True}
            if fn.__name__ == "send_keys_to_foreground":
                return {"success": True, "effect_verified": True}
            if fn.__name__ == "get_text":
                previous_send = any(
                    name == "send_keys_to_foreground" and values[0] == "{enter}"
                    for name, values, _options in calls
                )
                return {"success": True, "value": "" if previous_send else "ola"}
            raise AssertionError(fn.__name__)

    executor = CompoundActionExecutor(UiController())
    monkeypatch.setattr(wm, "focus_window", lambda _hwnd: True)
    monkeypatch.setattr(
        executor, "_editable",
        lambda hwnd, search=False: _async(({
            "name": "Mensagem", "automation_id": "composer",
            "control_type": "Edit", "enabled": True, "value": "",
        }, {"success": True})),
    )
    result = await executor._type_or_send(
        ActionContext("discord", "Discord", hwnd=55), "ola", send=True,
    )
    assert result["ok"] is True
    assert any(
        name == "send_keys_to_foreground" and values[0] == "{enter}"
        for name, values, _options in calls
    )


@pytest.mark.asyncio
async def test_search_focuses_address_types_and_submits(monkeypatch):
    from app.desktop import window_manager as wm

    calls = []

    class UiController:
        async def _uia_call(self, fn, *args, **kwargs):
            calls.append((fn.__name__, args, kwargs))
            if fn.__name__ == "send_keys_to_foreground":
                return {"success": True}
            if fn.__name__ == "set_text":
                return {"success": True, "effect_verified": True}
            if fn.__name__ == "get_text":
                return {"success": True, "value": "https://search/?q=Proxmox"}
            raise AssertionError(fn.__name__)

    executor = CompoundActionExecutor(UiController())
    monkeypatch.setattr(wm, "focus_window", lambda _hwnd: True)
    monkeypatch.setattr(
        executor, "_editable",
        lambda hwnd, search=False: _async(({
            "name": "Address and search bar", "automation_id": "address",
            "control_type": "Edit", "enabled": True, "value": "",
        }, {"success": True})),
    )
    result = await executor._search(
        ActionContext("chrome", "Google Chrome", hwnd=77), "Proxmox",
    )
    assert result["ok"] is True
    key_inputs = [values[0] for name, values, _ in calls
                  if name == "send_keys_to_foreground"]
    assert key_inputs == ["{ctrl+l}", "{enter}"]
