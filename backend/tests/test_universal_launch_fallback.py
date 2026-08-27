from __future__ import annotations

from pathlib import Path

import pytest

from app.desktop.control import DesktopController
from app.desktop.discovery import ApplicationCandidate, LaunchMethod
from app.desktop.universal_registry import UniversalAppRegistry
from app.events import EventBus


def candidate(method: str, target: str, source: str, confidence: float) -> ApplicationCandidate:
    return ApplicationCandidate(
        id="sampleapp",
        display_name="Sample App",
        source=source,
        launch_method=method,
        target=target,
        confidence=confidence,
    )


class StaticDiscovery:
    enabled = True

    def __init__(self, candidates: list[ApplicationCandidate]) -> None:
        self.candidates = candidates

    def index(self, force: bool = False) -> list[ApplicationCandidate]:
        return list(self.candidates)

    def candidates_for(self, app_id: str) -> list[ApplicationCandidate]:
        return [item for item in self.candidates if item.id == app_id]

    @staticmethod
    def revalidate(_candidate: ApplicationCandidate) -> bool:
        return True


def routes(tmp_path: Path) -> tuple[StaticDiscovery, list[ApplicationCandidate]]:
    values = [
        candidate(LaunchMethod.START_MENU, r"C:\Menu\Sample App.lnk", "start_menu", 0.75),
        candidate(LaunchMethod.EXE, r"C:\Apps\Sample\sample.exe", "app_paths:HKCU", 0.9),
        candidate(
            LaunchMethod.APP_USER_MODEL_ID,
            "Vendor.Sample_123!App",
            "get_start_apps",
            0.7,
        ),
    ]
    return StaticDiscovery(values), values


def test_registry_keeps_all_routes_and_persists_verified_preference(tmp_path: Path):
    discovery, values = routes(tmp_path)
    registry = UniversalAppRegistry(discovery=discovery, root=tmp_path / "registry")
    registry.refresh(force=True)

    assert len(registry.entries["sampleapp"].launch_options) == 3
    initial = registry.resolve_launch_candidates("Sample App")
    assert [item.launch_method for item in initial[:3]] == [
        LaunchMethod.START_MENU,
        LaunchMethod.EXE,
        LaunchMethod.APP_USER_MODEL_ID,
    ]
    assert any(item.launch_method == LaunchMethod.SHELL_EXECUTE for item in initial)

    registry.record_success(
        "sampleapp",
        alias_query="sample",
        launch_candidate=values[1],
    )
    restored = UniversalAppRegistry(discovery=discovery, root=tmp_path / "registry")
    assert restored.resolve_launch_candidates("sample")[0].launch_method == LaunchMethod.EXE
    restored.refresh(force=True)
    assert restored.resolve_launch_candidates("sample")[0].launch_method == LaunchMethod.EXE


@pytest.mark.asyncio
async def test_launcher_falls_through_only_after_failed_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    discovery, _values = routes(tmp_path)
    registry = UniversalAppRegistry(discovery=discovery, root=tmp_path / "registry")
    registry.refresh(force=True)
    controller = DesktopController(
        EventBus(),
        apps_path=tmp_path / "desktop_apps.yaml",
        dynamic_discovery=False,
        universal=registry,
    )
    attempted: list[str] = []

    async def fake_attempt(current, done, *, origin, expected_window=True):
        attempted.append(str(current.launch_method))
        if current.launch_method == LaunchMethod.EXE:
            return done(
                success=True,
                message="verified",
                execution_success=True,
                effect_verified=True,
                verification_status="VERIFIED",
                detail={"candidate": current.public_dict(), "pid": 4242},
            )
        return done(
            success=False,
            error_code="WINDOW_NOT_CONFIRMED",
            message="not verified",
            execution_success=True,
            effect_verified=False,
            verification_status="VERIFICATION_FAILED",
            detail={"candidate": current.public_dict()},
        )

    monkeypatch.setattr(controller, "_launch_candidate", fake_attempt)
    result, successful = await controller._launch_candidates_with_fallback(
        registry.resolve_launch_candidates("Sample App"),
        origin="test",
    )

    assert attempted == [LaunchMethod.START_MENU, LaunchMethod.EXE]
    assert successful is not None and successful.launch_method == LaunchMethod.EXE
    assert result["effect_verified"] is True
    assert len(result["attempts"]) == 2
