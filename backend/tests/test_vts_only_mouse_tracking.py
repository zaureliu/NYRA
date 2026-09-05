from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import pytest

from app.avatar.controller import AvatarState
from app.avatar.vtube_studio.models import MouseTrackingMode, VTSConnectionState, VTubeStudioConfig
from app.avatar.vtube_studio.mouse_tracking import MouseTrackingController
from app.avatar.vtube_studio.parameters import mouth_parameter_values, mouse_parameter_values
from app.avatar.vtube_studio.provider import VTubeStudioAvatarProvider
from app.api.models import VTSPresenceReport

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def tick(self, value: float = 1 / 30) -> None:
        self.value += value


def frame_for(mode: MouseTrackingMode, x: float, y: float, *, speaking: bool = False):
    clock = Clock()
    controller = MouseTrackingController(mode, clock=clock)
    clock.tick()
    return controller.update(x, y, speaking=speaking)


def test_default_mode_and_legacy_settings_migrate_to_vts_only():
    assert VTubeStudioConfig().mouse_tracking == MouseTrackingMode.HEAD_EYES
    migrated = VTubeStudioConfig.model_validate({"renderer": "INTERNAL", "cursor_attention": True})
    assert migrated.renderer == "VTUBE_STUDIO"
    assert migrated.mouse_tracking == MouseTrackingMode.HEAD_EYES
    assert "cursor_attention" not in migrated.model_dump(mode="json")


def test_legacy_avatar_assets_and_runtime_entrypoints_are_removed():
    assert not (ROOT / "frontend/src/avatar").exists()
    assert not (ROOT / "frontend/public/avatar").exists()
    assert not (ROOT / "backend/app/avatar/providers.py").exists()
    desktop = (ROOT / "frontend/src/desktop/DesktopApp.tsx").read_text(encoding="utf-8")
    settings = (ROOT / "frontend/src/components/Live2DSettings.tsx").read_text(encoding="utf-8")
    native = (ROOT / "desktop/src-tauri/src/spout_presence.cpp").read_text(encoding="utf-8")
    assert "AvatarRenderer" not in desktop
    assert 'option value="INTERNAL"' not in settings and '>AUTO<' not in settings
    assert "InternalActive" not in native and "FallbackInternal" not in native


def test_deadzone_clamp_direction_and_vertical_orientation():
    neutral = frame_for(MouseTrackingMode.HEAD_EYES, .03, -.02)
    assert (neutral.eye_x, neutral.eye_y, neutral.head_x, neutral.head_y) == (0, 0, 0, 0)
    bounded = frame_for(MouseTrackingMode.HEAD_EYES, 3, -4)
    assert 0 < bounded.eye_x <= 1
    assert -1 <= bounded.eye_y < 0
    assert 0 < bounded.head_x < bounded.eye_x
    assert bounded.eye_y < bounded.head_y < 0


def test_eyes_are_faster_than_head_and_smoothing_has_no_large_jump():
    clock = Clock()
    controller = MouseTrackingController(clock=clock)
    frames = []
    for _ in range(8):
        clock.tick()
        frames.append(controller.update(1, .5))
    assert all(0 <= frame.head_x < frame.eye_x <= 1 for frame in frames)
    assert max(b.eye_x - a.eye_x for a, b in zip(frames, frames[1:])) <= .251
    assert max(b.head_x - a.head_x for a, b in zip(frames, frames[1:])) <= .071


def test_tracking_modes_reset_only_the_parameters_they_own():
    mapping = {
        "eye_x": ["EyeBallX"], "eye_y": ["EyeBallY"],
        "head_x": ["FaceAngleX"], "head_y": ["FaceAngleY"],
    }
    eyes = frame_for(MouseTrackingMode.EYES, 1, 1)
    assert {value["id"] for value in mouse_parameter_values(eyes, mapping)} == {"EyeBallX", "EyeBallY"}

    controller = MouseTrackingController(MouseTrackingMode.HEAD_EYES, clock=Clock())
    controller.update(1, 1)
    controller.configure(MouseTrackingMode.EYES)
    reset_head = controller.update(1, 1)
    head_values = {value["id"]: value["value"] for value in mouse_parameter_values(reset_head, mapping)}
    assert head_values["FaceAngleX"] == 0 and head_values["FaceAngleY"] == 0

    controller.configure(MouseTrackingMode.OFF)
    off = controller.update(1, 1)
    assert all(value["value"] == 0 for value in mouse_parameter_values(off, mapping))
    assert mouse_parameter_values(controller.update(1, 1), mapping) == []


def test_missing_model_parameters_are_ignored_without_disabling_tracking():
    frame = frame_for(MouseTrackingMode.HEAD_EYES, -.8, .6)
    values = mouse_parameter_values(frame, {"eye_x": ["EyeBallX"]})
    assert len(values) == 1 and values[0]["id"] == "EyeBallX" and values[0]["value"] < 0


def test_mouse_does_not_overwrite_lip_sync_or_emotion_parameters():
    mapping = {
        "eye_x": ["EyeBallX"], "head_x": ["FaceAngleX"],
        "mouth_open": ["MouthOpen"], "amused": ["NyraEmotionAmused"],
    }
    mouse_ids = {value["id"] for value in mouse_parameter_values(frame_for(MouseTrackingMode.HEAD_EYES, 1, 0), mapping)}
    mouth_ids = {value["id"] for value in mouth_parameter_values(AvatarState(mouth_open=.7), mapping)}
    assert mouse_ids == {"EyeBallX", "FaceAngleX"}
    assert mouth_ids == {"MouthOpen"}
    assert mouse_ids.isdisjoint(mouth_ids | {"NyraEmotionAmused"})


def test_speaking_reduces_head_influence_but_not_eye_influence():
    regular = frame_for(MouseTrackingMode.HEAD_EYES, .4, .2)
    speaking = frame_for(MouseTrackingMode.HEAD_EYES, .4, .2, speaking=True)
    assert speaking.eye_x == regular.eye_x and speaking.eye_y == regular.eye_y
    assert speaking.head_x < regular.head_x and speaking.head_y < regular.head_y


def test_tracking_math_cost_is_bounded():
    clock = Clock()
    controller = MouseTrackingController(clock=clock)
    started = perf_counter()
    for index in range(5000):
        clock.tick()
        controller.update((index % 201 - 100) / 100, (100 - index % 201) / 100)
    average_ms = (perf_counter() - started) * 1000 / 5000
    assert average_ms < .1


def test_presence_report_schema_is_vts_only():
    report = VTSPresenceReport(state="VTS_UNAVAILABLE", alpha="UNKNOWN", vts_active=False)
    assert report.vts_active is False


@pytest.mark.asyncio
async def test_model_change_refreshes_tracking_capabilities():
    provider = VTubeStudioAvatarProvider(VTubeStudioConfig())
    provider.model = {"modelLoaded": True, "modelID": "old"}

    class FakeClient:
        async def call(self, kind, data=None):
            if kind == "CurrentModelRequest":
                return {"data": {"modelLoaded": True, "modelID": "new", "modelName": "New current model"}}
            if kind == "InputParameterListRequest":
                return {"data": {"defaultParameters": [{"name": "EyeBallX"}, {"name": "FaceAngleX"}]}}
            if kind == "HotkeysInCurrentModelRequest":
                return {"data": {"availableHotkeys": []}}
            if kind == "ExpressionStateRequest":
                return {"data": {"expressions": []}}
            return {"data": {}}

    provider.client = FakeClient()
    await provider._refresh_model_metadata()
    assert provider.model["modelID"] == "new"
    assert provider.mapping["eye_x"] == ["EyeBallX"]
    assert provider.mapping["head_x"] == ["FaceAngleX"]


@pytest.mark.asyncio
async def test_mode_change_reset_bypasses_frame_throttle():
    provider = VTubeStudioAvatarProvider(VTubeStudioConfig(mouse_tracking=MouseTrackingMode.HEAD_EYES))
    provider.state = VTSConnectionState.READY
    provider.mapping = {"eye_x": ["EyeBallX"], "head_x": ["FaceAngleX"]}
    calls = []

    class FakeClient:
        async def call(self, kind, data=None):
            calls.append(data["parameterValues"])
            return {"data": {}}

        async def close(self):
            return None

    provider.client = FakeClient()
    assert await provider.apply_cursor(AvatarState(), 1, 0)
    provider.config = provider.config.model_copy(update={"mouse_tracking": MouseTrackingMode.EYES})
    assert await provider.apply_cursor(AvatarState(), 1, 0)
    assert {value["id"]: value["value"] for value in calls[-1]}["FaceAngleX"] == 0
