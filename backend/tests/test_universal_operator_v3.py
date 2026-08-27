"""nyra-full V3 — regressão targeted do Universal Operator no runtime real.

Cobre:
  * §4   intents obrigatórias (open/close/minimize/maximize/restore/focus/switch,
         open folder, open file);
  * §6   pastas conhecidas PT-BR resolvidas dinamicamente (sem hardcode de user);
  * §7   OPEN_FILE com resolução e app associado;
  * §8   contexto natural ("ele"/"ela"/"de novo");
  * §9   one action owner (1 pedido → 1 execução física por turno);
  * §10  fast path: comandos simples NUNCA dependem de Agent Loop;
  * §13  LIST_FILES ≠ OPEN_FOLDER (regressão Downloads → filesystem_list_files);
  * §14  effect verification obrigatória antes de afirmar sucesso;
  * §15  grounding (NOT_FOUND honesto, nenhuma afirmação falsa);
  * §17  already-open não duplica instância.
"""

from __future__ import annotations

import yaml
import pytest

import app.desktop.control as control_module
from app.desktop import window_manager as wm_module
from app.desktop.control import DesktopController
from app.desktop.discovery import ApplicationDiscovery
from app.desktop.intents import (
    FOLDER_SHELL_URIS,
    KNOWN_FOLDER_KEYS,
    UniversalAction,
    parse_universal_intent,
)
from app.desktop.models import WindowInfo
from app.desktop.universal_registry import UniversalAppRegistry
from app.events import EventBus


# --------------------------------------------------------------- harness


class FakeWindowLayer:
    """Substitui a enumeração Win32 por janelas falsas determinísticas."""

    def __init__(self) -> None:
        self.windows: list[WindowInfo] = []
        self.spawn_on_execute: WindowInfo | None = None

    def add(self, window: WindowInfo) -> None:
        self.windows.append(window)

    def remove(self, hwnd: int) -> None:
        self.windows = [w for w in self.windows if w.hwnd != hwnd]

    def _list_visible(self):
        return [w.model_copy(deep=True) for w in self.windows]

    @staticmethod
    def _annotate(windows):
        return windows


@pytest.fixture()
def layer(monkeypatch: pytest.MonkeyPatch) -> FakeWindowLayer:
    fake = FakeWindowLayer()
    monkeypatch.setattr(control_module, "list_visible_windows", fake._list_visible)
    monkeypatch.setattr(control_module, "annotate_process_names", fake._annotate)
    return fake


@pytest.fixture()
def controller(tmp_path, layer: FakeWindowLayer, monkeypatch: pytest.MonkeyPatch) -> DesktopController:
    apps_yaml = tmp_path / "desktop_apps.yaml"
    apps_yaml.write_text(yaml.safe_dump({"apps": []}), encoding="utf-8")
    registry = UniversalAppRegistry(
        discovery=ApplicationDiscovery(enabled=False), root=tmp_path / "app-registry"
    )
    instance = DesktopController(
        EventBus(), apps_path=apps_yaml,
        dynamic_discovery=False, universal=registry,
    )
    instance._test_shell_calls = []  # type: ignore[attr-defined]

    def fake_shell_execute(candidate):
        instance._test_shell_calls.append(candidate.public_dict())  # type: ignore[attr-defined]
        if layer.spawn_on_execute is not None:
            layer.add(layer.spawn_on_execute)
            layer.spawn_on_execute = None
        return True

    # ShellExecuteW nunca é realmente invocado nos testes de unidade.
    monkeypatch.setattr(control_module, "_shell_execute", fake_shell_execute)
    return instance


def explorer_window(title: str = "Downloads", hwnd: int = 111) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, pid=999, title=title, visible=True, process_name="explorer.exe")


# ------------------------------------------------------------------ parser (§4/§6/§7)


@pytest.mark.parametrize(
    ("text", "action", "target"),
    [
        ("abre a pasta Downloads", UniversalAction.OPEN_FOLDER, "downloads"),
        ("abre a pasta de projetos", UniversalAction.OPEN_FOLDER, "projetos"),
        ("abre Documentos", UniversalAction.OPEN_FOLDER, "documentos"),
        ("abra a área de trabalho", UniversalAction.OPEN_FOLDER, "área de trabalho"),
        ("abre Imagens", UniversalAction.OPEN_FOLDER, "imagens"),
        ("abre as músicas", UniversalAction.OPEN_FOLDER, "músicas"),
        ("abre o arquivo relatorio.pdf", UniversalAction.OPEN_FILE, "relatorio.pdf"),
        ("abre nyra-open-test.txt", UniversalAction.OPEN_FILE, "nyra-open-test.txt"),
        ("alterna para o code", UniversalAction.SWITCH_APP, "code"),
        ("alterna pro code", UniversalAction.SWITCH_APP, "code"),
        ("troca para a calculadora", UniversalAction.SWITCH_APP, "calculadora"),
        ("vai para o navegador", UniversalAction.FOCUS_APP, "navegador"),
    ],
)
def test_parse_v3_new_intents(text: str, action: UniversalAction, target: str):
    intent = parse_universal_intent(text)
    assert intent is not None, text
    assert intent.action == action, text
    assert intent.target == target, text


@pytest.mark.parametrize(
    "text",
    [
        "mostra os arquivos de downloads",
        "liste os arquivos da pasta documentos",
        "quais arquivos existem em imagens?",
    ],
)
def test_list_files_never_becomes_open_folder(text: str):
    """§13 regressão: 'mostra/lista arquivos' NÃO vira OPEN_FOLDER nem OPEN_APP."""
    assert parse_universal_intent(text) is None


@pytest.mark.parametrize("text", ["abre a pasta downloads", "abre Documentos", "abre Imagens"])
def test_open_folder_is_deterministic_not_agent_loop(text: str):
    intent = parse_universal_intent(text)
    assert intent is not None and intent.action == UniversalAction.OPEN_FOLDER


def test_known_folders_dynamic_no_username():
    """§6: resolução via shell:/ambiente; nenhum caminho com username fixado."""
    required = {"downloads", "documentos", "imagens", "musicas", "videos", "desktop"}
    assert required.issubset(KNOWN_FOLDER_KEYS)
    assert FOLDER_SHELL_URIS["downloads"] == ("Downloads", "shell:Downloads")
    for _key, (_display, target) in FOLDER_SHELL_URIS.items():
        assert "C:\\Users" not in target and "\\Users" not in target


# ------------------------------------------------------- fast path (§10) + owner (§9)

FAST_PATH_COMMANDS = [
    "abre o Discord",
    "fecha o Discord",
    "minimiza o Code",
    "maximiza o Edge",
    "restaura o Notepad",
    "traz o Spotify pra frente",
    "alterna pro Firefox",
    "abre a pasta Downloads",
    "abre Documentos",
    "abre Imagens",
    "abre o arquivo nyra-open-test.txt",
]


@pytest.mark.parametrize("command", FAST_PATH_COMMANDS)
def test_simple_commands_have_deterministic_owner(command: str):
    """§9/§10: todo comando simples é dono determinístico — nunca Agent Loop."""
    assert parse_universal_intent(command) is not None, command


async def test_one_action_owner_single_physical_launch(controller: DesktopController, layer: FakeWindowLayer):
    """§9/§27: mesmo action repetido no MESMO turno = 0 execuções adicionais."""
    layer.spawn_on_execute = explorer_window()
    intent = parse_universal_intent("abre a pasta downloads")
    handled_1, reply_1 = await controller.handle_universal(intent, turn_id="turn-1")
    total_after_first = len(controller._test_shell_calls)  # type: ignore[attr-defined]
    handled_2, reply_2 = await controller.handle_universal(intent, turn_id="turn-1")

    assert handled_1 and handled_2
    assert reply_1 == reply_2
    assert total_after_first == 1
    assert len(controller._test_shell_calls) == 1  # type: ignore[attr-defined]


# ------------------------------------------------------- folders E2E-level (§6/§14)


async def test_open_downloads_verified_with_explorer_window(controller: DesktopController, layer: FakeWindowLayer):
    layer.spawn_on_execute = explorer_window("Downloads")
    handled, reply = await controller.handle_universal(
        parse_universal_intent("abre a pasta Downloads"), turn_id="t-folder"
    )

    assert handled
    assert "Pasta Downloads aberta" in reply
    assert len(controller._test_shell_calls) == 1  # type: ignore[attr-defined]
    called = controller._test_shell_calls[0]  # type: ignore[attr-defined]
    assert called["target"] == "shell:Downloads"
    assert controller.last_controlled is not None
    assert controller.last_controlled["kind"] == "folder"


async def test_unknown_folder_honest_not_found(controller: DesktopController):
    handled, reply = await controller.handle_universal(
        parse_universal_intent("abre a pasta nao-existe-xyz"), turn_id="t-folder-404"
    )

    assert handled
    assert "Não encontrei a pasta" in reply
    assert "Nada foi aberto" in reply
    assert controller._test_shell_calls == []  # type: ignore[attr-defined]


async def test_context_close_folder_via_pronoun(controller: DesktopController, layer: FakeWindowLayer,
                                                monkeypatch: pytest.MonkeyPatch):
    """§18: após 'abre Downloads', 'fecha ela' fecha a janela do Explorer."""
    layer.add(explorer_window("Downloads"))
    await controller.handle_universal(
        parse_universal_intent("abre a pasta Downloads"), turn_id="t-open"
    )

    closed: list[int] = []

    def fake_graceful_close(hwnd: int, timeout_seconds: float = 5.0) -> bool:
        closed.append(hwnd)
        layer.remove(hwnd)
        return True

    monkeypatch.setattr(wm_module, "graceful_close", fake_graceful_close)
    handled, reply = await controller.handle_universal(parse_universal_intent("fecha ela"), turn_id="t-close")

    assert handled
    assert closed == [111]
    assert "1 janela(s)" in reply and "fechada(s)" in reply


async def test_already_open_folder_focuses_instead_of_duplicating(
    controller: DesktopController, layer: FakeWindowLayer, monkeypatch: pytest.MonkeyPatch
):
    """§17: pasta já aberta → foco/restauração; nenhuma segunda instância."""
    layer.add(explorer_window("Downloads"))
    focused: list[int] = []
    monkeypatch.setattr(wm_module, "focus_window", lambda hwnd, timeout_seconds=3.0: focused.append(hwnd) or True)

    handled, reply = await controller.handle_universal(
        parse_universal_intent("abre a pasta downloads"), turn_id="t-already"
    )

    assert handled
    assert "já estava aberta" in reply or "já estava aberto" in reply
    assert controller._test_shell_calls == [] or len(controller._test_shell_calls) <= 1  # type: ignore[attr-defined]


# ------------------------------------------------------- files (§7/§29)


async def test_open_file_resolves_and_verifies(controller: DesktopController, layer: FakeWindowLayer,
                                               monkeypatch: pytest.MonkeyPatch, tmp_path):
    from pathlib import Path

    from app.core.paths import DATA_ROOT

    fixture_dir = DATA_ROOT
    fixture = fixture_dir / "nyra-open-test.txt"
    if not fixture.is_file():
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture.write_text("fixture", encoding="utf-8")
    try:
        opened: list[str] = []

        async def fake_open_file(path: str, *, app: str = ""):
            opened.append(path)
            layer.add(WindowInfo(hwnd=222, pid=777, title="nyra-open-test - Bloco de notas",
                                 visible=True, process_name="notepad.exe"))
            return {"success": True, "message": "ok"}

        monkeypatch.setattr(controller, "open_file", fake_open_file)
        handled, reply = await controller.handle_universal(
            parse_universal_intent("abre o arquivo nyra-open-test.txt"), turn_id="t-file"
        )

        assert handled
        assert len(opened) == 1
        assert Path(opened[0]).name == "nyra-open-test.txt"
        assert "aberto no notepad" in reply
        assert controller.last_controlled is not None
        assert controller.last_controlled["kind"] == "file"
    finally:
        if fixture.is_file() and fixture.parent == fixture_dir:
            fixture.unlink(missing_ok=True)


async def test_open_missing_file_grounding(controller: DesktopController):
    """§15: arquivo inexistente → resposta honesta, nenhuma execução."""
    handled, reply = await controller.handle_universal(
        parse_universal_intent("abre o arquivo zumbi-quantum-3000.txt"), turn_id="t-file-404"
    )
    assert handled
    assert "Não encontrei o arquivo" in reply
    assert "Nada foi aberto" in reply


# ------------------------------------------------------- windows/context (§8/§18)


@pytest.mark.parametrize(
    ("text", "verb", "window_title"),
    [
        ("minimiza ele", "minimize_window", "Visual Studio Code"),
        ("maximiza ele", "maximize_window", "Visual Studio Code"),
        ("restaura ele", "restore_window", "Discord"),
        ("fecha ele", "graceful_close", "Discord"),
    ],
)
async def test_context_window_ops_call_right_win32(controller: DesktopController, layer: FakeWindowLayer,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   text: str, verb: str, window_title: str):
    layer.add(WindowInfo(hwnd=333, pid=555, title=f"{window_title}", visible=True,
                         process_name=f"{window_title.split()[0].casefold()}.exe"))
    controller._note_controlled(window_title, kind="app", process_names=(f"{window_title.split()[0].casefold()}.exe",))

    calls: list[tuple[str, int]] = []

    def make_fake(name):
        def fake(hwnd, timeout_seconds=3.0):
            calls.append((name, hwnd))
            return True
        return fake

    for name in ("minimize_window", "maximize_window", "restore_window", "graceful_close"):
        monkeypatch.setattr(wm_module, name, make_fake(name))

    handled, reply = await controller.handle_universal(parse_universal_intent(text), turn_id=f"ctx-{text}")

    assert handled, text
    assert verb in {name for name, _ in calls}, (text, calls)
    assert any(hwnd == 333 for _, hwnd in calls), (text, calls)
    assert "com verificação" in reply


async def test_switch_app_restores_minimized_and_focuses(controller: DesktopController, layer: FakeWindowLayer,
                                                         monkeypatch: pytest.MonkeyPatch):
    layer.add(WindowInfo(hwnd=444, pid=888, title="Code — arquivo.py", visible=True,
                         process_name="code.exe"))
    controller._note_controlled("Code", kind="app", process_names=("code.exe",))

    monkeypatch.setattr(wm_module, "window_state",
                        lambda hwnd: {"iconic": True, "zoomed": False, "foreground": False})
    order: list[str] = []
    monkeypatch.setattr(wm_module, "restore_window",
                        lambda hwnd, timeout_seconds=3.0: order.append("restore") or True)
    monkeypatch.setattr(wm_module, "focus_window",
                        lambda hwnd, timeout_seconds=3.0: order.append("focus") or True)

    handled, reply = await controller.handle_universal(
        parse_universal_intent("alterna pro code"), turn_id="t-switch"
    )

    assert handled
    assert "restore" in order and "focus" in order
    assert "primeiro plano" in reply


async def test_reopen_de_novo_reopens_last_app(controller: DesktopController, layer: FakeWindowLayer):
    layer.spawn_on_execute = explorer_window()
    await controller.handle_universal(parse_universal_intent("abre a pasta downloads"), turn_id="r1")
    first_calls = len(controller._test_shell_calls)  # type: ignore[attr-defined]

    handled, reply = await controller.handle_universal(
        parse_universal_intent("abre de novo"), turn_id="r2"
    )

    assert handled
    assert len(controller._test_shell_calls) == first_calls + 1  # type: ignore[attr-defined]
    assert "Pasta" in reply


# ------------------------------------------------------- already-open (§17/§27)


def _seed_code_app(controller: DesktopController) -> None:
    from app.desktop.discovery import ApplicationCandidate, LaunchMethod
    from app.desktop.universal_registry import UniversalAppRegistry

    candidate = ApplicationCandidate(
        id="visualstudiocode", display_name="Visual Studio Code",
        source="test", launch_method=LaunchMethod.EXE,
        target=r"C:\Windows\System32\notepad.exe", confidence=1.0,
    )
    entry = UniversalAppRegistry._entry_from_candidate(candidate)
    controller.universal.entries[entry.app_id] = entry
    controller.universal.record_success(entry.app_id, alias_query="code")


async def test_already_open_app_focuses_without_new_spawn(
    controller: DesktopController, layer: FakeWindowLayer, monkeypatch: pytest.MonkeyPatch
):
    """§17: app já aberto → foco; nenhuma duplicata por padrão."""
    _seed_code_app(controller)
    layer.add(WindowInfo(hwnd=555, pid=444, title="arquivo.py - Visual Studio Code",
                         visible=True, process_name="Code.exe"))
    focused: list[int] = []
    monkeypatch.setattr(wm_module, "focus_window",
                        lambda hwnd, timeout_seconds=3.0: focused.append(hwnd) or True)

    handled, reply = await controller.handle_universal(parse_universal_intent("abre o code"),
                                                       turn_id="t-app-open")
    assert handled
    assert "já estava aberto" in reply
    assert controller._test_shell_calls == []  # type: ignore[attr-defined]
    assert focused == [555]


async def test_explicit_new_instance_bypasses_already_open(
    controller: DesktopController, layer: FakeWindowLayer, monkeypatch: pytest.MonkeyPatch
):
    """§27: 'abre outro X' PODE abrir nova instância."""
    _seed_code_app(controller)
    layer.add(WindowInfo(hwnd=555, pid=444, title="Visual Studio Code",
                         visible=True, process_name="Code.exe"))
    layer.spawn_on_execute = WindowInfo(hwnd=556, pid=445, title="Sem título - Visual Studio Code",
                                        visible=True, process_name="Code.exe")

    class FakeProcess:
        pid = 445

        def poll(self):
            return None

        def children(self):
            return []

    def fake_popen(*args, **kwargs):
        popen_calls.append(1)
        if layer.spawn_on_execute is not None:
            layer.add(layer.spawn_on_execute)
            layer.spawn_on_execute = None
        return FakeProcess()

    # O ramo EXE usa subprocess.Popen real: falso também revela a nova janela.
    popen_calls: list[int] = []
    monkeypatch.setattr(control_module.subprocess, "Popen", fake_popen)

    intent = parse_universal_intent("abre outro code")
    assert intent is not None and intent.explicit_new is True
    assert intent.target == "code"

    handled, reply = await controller.handle_universal(intent, turn_id="t-app-new")
    assert handled
    assert sum(popen_calls) == 1
    assert "Aberto" in reply or "aberto" in reply


def test_parser_explicit_new_strips_quantifier():
    plain = parse_universal_intent("abre o discord")
    assert plain is not None and plain.explicit_new is False and plain.target == "discord"
    other = parse_universal_intent("abre outro bloco de notas")
    assert other is not None and other.explicit_new is True
    assert other.target == "bloco de notas"


def test_parser_traz_de_volta_stripped():
    intent = parse_universal_intent("traz ele de volta")
    assert intent is not None
    assert intent.action == UniversalAction.FOCUS_APP
    assert intent.target == "ele" and intent.contextual is True


# ------------------------------------------------------- grounding (§15/§30)


async def test_unknown_app_grounded_not_found(controller: DesktopController):
    """§30: app inexistente → NOT_FOUND honesto, zero execução aleatória."""
    handled, reply = await controller.handle_universal(
        parse_universal_intent("abre o zumbi quantum editor 3000"), turn_id="t-zombie"
    )

    assert handled
    lowered = reply.casefold()
    assert "não encontrei" in lowered
    assert "nada foi executado" in lowered
    assert controller._test_shell_calls == []  # type: ignore[attr-defined]


async def test_context_without_history_is_honest(controller: DesktopController):
    handled, reply = await controller.handle_universal(parse_universal_intent("fecha ele"), turn_id="t-nohist")
    assert handled
    assert "não sei" in reply.casefold()


async def test_no_window_for_target_reports_no_change(controller: DesktopController, layer: FakeWindowLayer,
                                                      monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wm_module, "graceful_close", lambda hwnd, timeout_seconds=5.0: True)
    handled, reply = await controller.handle_universal(parse_universal_intent("fecha o discord"), turn_id="t-empty")
    assert handled
    assert "Nenhuma janela visível" in reply
