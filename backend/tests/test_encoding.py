"""Encoding regression tests (prompt8 §162-§175).

The UI must never render mojibake ('Configurações' as 'Configurações').
These tests fail the suite if double-encoded text, replacement characters or
non-UTF-8 sources appear in user-facing files.
"""

from __future__ import annotations

from pathlib import Path

from app.core.encoding_audit import PROJECT_ROOT, iter_targets, scan_file

_UI_FILES = [
    Path("frontend/src/App.tsx"),
    Path("frontend/src/ops/Sidebar.tsx"),
    Path("frontend/src/ops/TopStatusBar.tsx"),
    Path("frontend/src/ops/pages/CapabilitiesPage.tsx"),
    Path("frontend/src/ops/pages/IntegrationsPage.tsx"),
    Path("frontend/src/ops/pages/SentinelPage.tsx"),
    Path("frontend/src/ops/pages/VoicePage.tsx"),
    Path("frontend/src/ops/pages/SettingsPageV3.tsx"),
    Path("frontend/src/components/AudioSettings.tsx"),
    Path("frontend/src/desktop/DesktopApp.tsx"),
    Path("frontend/index.html"),
    Path("frontend/desktop.html"),
]


def test_no_mojibake_in_ui_sources():
    offenders: list[str] = []
    for rel in _UI_FILES:
        path = PROJECT_ROOT / rel
        assert path.is_file(), f"arquivo esperado ausente: {rel}"
        issues = scan_file(path)
        if issues:
            offenders.append(f"{rel}: {issues}")
    assert not offenders, f"Mojibake detectado nos arquivos de UI: {offenders}"


def test_no_mojibake_across_backend_app():
    offenders = []
    for path in iter_targets(PROJECT_ROOT / "backend" / "app"):
        if path.name in {"encoding_audit.py", "exotic_scan.py"}:
            continue  # contêm as classes de caracteres da própria auditoria
        issues = scan_file(path)
        if issues:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {issues}")
    assert not offenders, f"Encoding inválido no backend: {offenders}"


def test_html_declares_utf8_charset():
    for rel in ("frontend/index.html", "frontend/desktop.html"):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert 'charset="UTF-8"' in text or "charset='UTF-8'" in text, f"{rel} sem charset UTF-8"


def test_acceptance_strings_are_clean():
    """§175: os rótulos reais da UI V3 precisam existir em PT-BR correto."""
    app = (PROJECT_ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    sidebar = (PROJECT_ROOT / "frontend/src/ops/Sidebar.tsx").read_text(encoding="utf-8")
    capabilities_page = (
        PROJECT_ROOT / "frontend/src/ops/pages/CapabilitiesPage.tsx"
    ).read_text(encoding="utf-8")

    # Rótulos do shell V3
    for needle in ("Conversa", "Capabilities", "Autonomia", "Tarefas", "Homelab"):
        assert needle in sidebar, f"Rótulo de navegação ausente: {needle!r}"
    for needle in ("Configurações", "Integrações", "Voz", "Sobre", "Developer"):
        assert needle in sidebar, f"Rótulo de navegação ausente: {needle!r}"

    # Rótulos com acento nas páginas V3 (mojibake quebraria a igualdade)
    overview_page = (
        PROJECT_ROOT / "frontend/src/ops/pages/OverviewPage.tsx"
    ).read_text(encoding="utf-8")
    assert "Visão geral" in overview_page, "'Visão geral' ausente do Overview"
    assert "Feature Control Center" in capabilities_page
    assert "habilitada" in capabilities_page or "desabilitada" in capabilities_page

    settings_v3 = (
        PROJECT_ROOT / "frontend/src/ops/pages/SettingsPageV3.tsx"
    ).read_text(encoding="utf-8")
    for needle in ("Geral", "Privacidade", "Homelab", "Automação", "fonte única"):
        assert needle.lower() in settings_v3.lower(), f"Rótulo ausente em Settings V3: {needle!r}"
