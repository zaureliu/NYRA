"""kazumi-full: Universal Operator — intents, registry, dedup, janelas."""
from __future__ import annotations

import pytest

from app.desktop.discovery import ApplicationCandidate, ApplicationDiscovery, LaunchMethod, normalize
from app.desktop.intents import UniversalAction, parse_universal_intent
from app.desktop.universal_registry import UniversalAppEntry, UniversalAppRegistry


# ------------------------------------------------------------------ intents

@pytest.mark.parametrize(
    ("text", "action", "target"),
    [
        ("abre o code", UniversalAction.OPEN_APP, "code"),
        ("Abre o Visual Studio Code.", UniversalAction.OPEN_APP, "visual studio code"),
        ("abra vscode", UniversalAction.OPEN_APP, "vscode"),
        ("inicia o terminal", UniversalAction.OPEN_APP, "terminal"),
        ("fecha o code", UniversalAction.CLOSE_APP, "code"),
        ("feche o navegador", UniversalAction.CLOSE_APP, "navegador"),
        ("minimiza o discord", UniversalAction.MINIMIZE_APP, "discord"),
        ("maximiza o edge", UniversalAction.MAXIMIZE_APP, "edge"),
        ("restaura a calculadora", UniversalAction.RESTORE_APP, "calculadora"),
        ("traz o spotify para frente", UniversalAction.FOCUS_APP, "spotify"),
        ("vai pro spotify", UniversalAction.FOCUS_APP, "spotify"),
        ("foca o code", UniversalAction.FOCUS_APP, "code"),
        ("Kazumi, abre o bloco de notas", UniversalAction.OPEN_APP, "bloco de notas"),
        ("abre a pasta downloads", UniversalAction.OPEN_FOLDER, "downloads"),
        ("abre esse arquivo", UniversalAction.OPEN_FILE, "esse arquivo"),
    ],
)
def test_parse_universal_intent(text: str, action: UniversalAction, target: str):
    intent = parse_universal_intent(text)
    assert intent is not None
    assert intent.action == action
    assert intent.target == target


@pytest.mark.parametrize(
    "text",
    [
        "oi tudo bem?",
        "abre o google",
        "pesquisa por fastapi",
        "abre meu painel do proxmox",
        "",
        "verifica o home assistant",
    ],
)
def test_non_app_intents_return_none(text: str):
    assert parse_universal_intent(text) is None


def test_contextual_target_flagged():
    intent = parse_universal_intent("fecha ele")
    assert intent is not None and intent.action == UniversalAction.CLOSE_APP
    assert intent.contextual is True


# ----------------------------------------------------------------- registry

def _candidate(app_id: str, display: str, target: str, conf: float = 0.9) -> ApplicationCandidate:
    return ApplicationCandidate(
        id=normalize(app_id), display_name=display, source="test",
        launch_method=LaunchMethod.EXE, target=target, confidence=conf,
    )


def test_registry_resolve_fast_exact_and_learned(tmp_path):
    discovery = ApplicationDiscovery(enabled=False)
    registry = UniversalAppRegistry(discovery=discovery, root=tmp_path / "reg")
    candidate = _candidate("visualstudiocode", "Visual Studio Code", r"C:\Program Files\Microsoft VS Code\Code.exe")
    entry = UniversalAppRegistry._entry_from_candidate(candidate)
    registry.entries[entry.app_id] = entry
    registry.record_success(entry.app_id, alias_query="code")

    fast = registry.resolve_fast("code")
    assert fast is not None and fast.display_name == "Visual Studio Code"
    # alias aprendido persiste
    registry2 = UniversalAppRegistry(discovery=discovery, root=tmp_path / "reg")
    assert registry2.learned_aliases.get(normalize("code")) == entry.app_id


def test_registry_record_failure_keeps_stats(tmp_path):
    registry = UniversalAppRegistry(discovery=ApplicationDiscovery(enabled=False), root=tmp_path / "r2")
    candidate = _candidate("calc", "Calculadora", "calc.exe")
    entry = UniversalAppRegistry._entry_from_candidate(candidate)
    registry.entries[entry.app_id] = entry
    before = entry.launch_count
    registry.record_failure(entry.app_id)
    assert entry.launch_count == before


def test_generic_aliases_seed():
    from app.desktop.universal_registry import build_aliases

    entry = UniversalAppEntry(app_id="x", display_name="Code", executable="Code.exe")
    aliases = build_aliases(entry)
    assert "vs code" in aliases and "code.exe" in aliases
