from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.avatar import AvatarController, AvatarState
from app.avatar.vtube_studio.models import VTSConnectionState, VTSEmotionBinding, VTubeStudioConfig
from app.avatar.vtube_studio.provider import VTubeStudioAvatarProvider
from app.emotional_presence import EmotionPresentationCoordinator, VoicePresentationAdapter
from app.emotional_presence.models import EmotionalPresenceSettings, EmotionalPresenceSettingsUpdate, VoiceEmotionSupport
from app.events import EventBus, EventType
from app.persona_runtime import NyraEmotion, PersonaRuntime
from app.speech.tts import TtsCapabilities


class FakeVoiceProvider:
    name = "local"
    active_provider = "local"
    active_voice = "NYRA_FIXED_VOICE"
    default_voice = "NYRA_FIXED_VOICE"

    def __init__(self, capabilities: TtsCapabilities) -> None:
        self._capabilities = capabilities

    def capabilities(self) -> TtsCapabilities:
        return self._capabilities


class FakeVTS:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.current = {"kind": "neutral", "target": None, "applied": True, "model_id": "fake-model"}

    async def apply_emotion(self, emotion: str, intensity: float, transition: dict):
        self.calls.append((emotion, intensity))
        self.current = {
            "kind": "expression" if emotion == "happy" else "neutral",
            "target": "NYRA_HAPPY" if emotion == "happy" else None,
            "applied": emotion in {"neutral", "happy"},
            "fallback": None if emotion in {"neutral", "happy"} else "model_has_no_compatible_emotion_capability",
            "model_id": "fake-model",
        }
        return self.current

    def status(self):
        return {
            "state": "READY", "model_loaded": True, "model": "Fake", "model_id": "fake-model",
            "hotkeys": [], "expressions": [], "emotion_capabilities": {},
            "last_emotion_presentation": self.current,
        }


async def build_runtime(tmp_path: Path):
    bus = EventBus()
    runtime = PersonaRuntime(tmp_path / "nyra.db", bus)
    await runtime.start()
    avatar = AvatarController(bus)
    vts = FakeVTS()
    voice = FakeVoiceProvider(TtsCapabilities(supports_native_speed=True, voice_selection=True, pt_br=True))
    coordinator = EmotionPresentationCoordinator(
        bus, runtime, provider_getter=lambda: voice, avatar=avatar, vtube_studio=vts,
    )
    coordinator.settings = EmotionalPresenceSettings()
    await coordinator.start()
    return bus, runtime, avatar, vts, voice, coordinator


@pytest.mark.asyncio
async def test_single_persona_emotion_reaches_text_voice_internal_avatar_and_vts(tmp_path: Path):
    bus, runtime, avatar, vts, _voice, coordinator = await build_runtime(tmp_path)

    await coordinator.controlled_transition(NyraEmotion.HAPPY, .44)
    context = await runtime.build_context("continue")
    status = await coordinator.status()

    assert runtime.emotion.primary == NyraEmotion.HAPPY
    assert "primary=happy; intensity=0.44" in context
    assert status["emotion"] == "happy" and status["intensity"] == .44
    assert status["voice"]["emotion"] == "happy" and status["voice"]["intensity"] == .44
    assert status["avatar"]["emotion"] == "happy" and status["avatar"]["intensity"] == .44
    assert avatar.state.expression == "happy" and avatar.state.emotion_intensity == .44
    assert vts.calls[-1] == ("happy", .44)
    assert any(item.type == EventType.NYRA_EMOTIONAL_PRESENCE_SYNCED for item in bus.history())

    # THINKING/LISTENING are operational and must not erase the emotion.
    await avatar.mode("thinking")
    await avatar.mode("listening")
    assert avatar.state.expression == "happy"

    await coordinator.stop(); await runtime.stop()


@pytest.mark.asyncio
async def test_transition_metadata_and_barge_in_close_mouth_without_erasing_emotion(tmp_path: Path):
    bus, runtime, avatar, _vts, _voice, coordinator = await build_runtime(tmp_path)
    await coordinator.controlled_transition("warning", .55)
    assert coordinator.current.transition.transition_ms == 260
    assert coordinator.current.transition.minimum_hold_ms == 1200
    await avatar.update(mouth_open=.8)
    await bus.publish(EventType.SPEECH_CANCELLED, reason="barge_in")
    assert avatar.state.mouth_open == 0
    assert avatar.state.expression == "focused"
    assert coordinator.current.emotion == NyraEmotion.WARNING
    await coordinator.stop(); await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("emotion", "expression"), [
    ("neutral", "neutral"), ("happy", "happy"), ("concerned", "concerned"),
    ("warning", "focused"), ("amused", "amused"),
])
async def test_controlled_states_share_emotion_and_intensity(tmp_path: Path, emotion: str, expression: str):
    _bus, runtime, avatar, vts, _voice, coordinator = await build_runtime(tmp_path)
    await coordinator.controlled_transition(emotion, .35)
    assert runtime.emotion.primary.value == emotion
    assert coordinator.current.emotion.value == emotion and coordinator.current.intensity == .35
    assert avatar.state.expression == expression and avatar.state.emotion_intensity == .35
    assert vts.calls[-1] == (emotion, .35)
    await coordinator.stop(); await runtime.stop()


@pytest.mark.asyncio
async def test_settings_are_local_and_sync_overhead_is_bounded(tmp_path: Path, monkeypatch):
    _bus, runtime, _avatar, _vts, _voice, coordinator = await build_runtime(tmp_path)
    saved = []
    monkeypatch.setattr("app.emotional_presence.coordinator.save_settings", lambda value: saved.append(value))
    await coordinator.update_settings(EmotionalPresenceSettingsUpdate(voice_expression=False))
    for index in range(25):
        await coordinator.controlled_transition("happy" if index % 2 else "focused", .3)
    status = await coordinator.status()
    assert saved and status["settings"]["voice_expression"] is False
    assert status["performance"]["average_sync_ms"] < 100
    await coordinator.stop(); await runtime.stop()


def test_voice_adapter_reports_real_capabilities_and_keeps_one_voice_identity():
    adapter = VoicePresentationAdapter()
    partial = FakeVoiceProvider(TtsCapabilities(supports_native_speed=True))
    full = FakeVoiceProvider(TtsCapabilities(supports_emotion=True, supports_styles=True, style_instructions=True))
    none = FakeVoiceProvider(TtsCapabilities())
    identities = set()

    for emotion in ("neutral", "happy", "concerned", "warning", "amused"):
        built = adapter.build_voice_style(emotion, .45, {}, partial)
        identities.add(built.presentation.voice_identity)
        assert built.presentation.emotion.value == emotion
        assert built.presentation.emotion_support == VoiceEmotionSupport.PARTIAL
        assert built.presentation.acoustic_emotion == "neutral"
        assert built.presentation.pitch_adjustment_hz == 0
    assert identities == {"NYRA_FIXED_VOICE"}
    assert adapter.build_voice_style("happy", .4, {}, full).presentation.emotion_support == VoiceEmotionSupport.FULL
    unsupported = adapter.build_voice_style("happy", .4, {}, none).presentation
    assert unsupported.emotion_support == VoiceEmotionSupport.NONE and unsupported.degraded


class FakeVTSClient:
    def __init__(self) -> None:
        self.socket = object()
        self.model_id = "model-1"
        self.with_capabilities = True
        self.calls: list[tuple[str, dict]] = []
        self.requests_sent = 0
        self.last_message_type = None

    async def close(self):
        self.socket = None

    async def call(self, kind: str, data=None):
        payload = dict(data or {})
        self.calls.append((kind, payload)); self.requests_sent += 1; self.last_message_type = kind
        if kind == "CurrentModelRequest":
            return {"data": {"modelLoaded": True, "modelName": "Current Model", "modelID": self.model_id}}
        if kind == "InputParameterListRequest":
            values = [{"name": "MouthOpen"}]
            if self.with_capabilities: values.append({"name": "NyraAmused"})
            return {"data": {"defaultParameters": values}}
        if kind == "HotkeysInCurrentModelRequest":
            hotkeys = [{"hotkeyID": "happy-id", "name": "NYRA_HAPPY", "type": "ToggleExpression"}] if self.with_capabilities else []
            return {"data": {"availableHotkeys": hotkeys}}
        if kind == "ExpressionStateRequest":
            expressions = [{"file": "NYRA_CONCERNED.exp3.json", "name": "NYRA_CONCERNED", "active": False}] if self.with_capabilities else []
            return {"data": {"expressions": expressions}}
        return {"data": {}}


@pytest.mark.asyncio
async def test_vts_frame_update_never_performs_connection_work():
    provider = VTubeStudioAvatarProvider(VTubeStudioConfig(enabled=True, renderer="VTUBE_STUDIO"))
    provider.state = VTSConnectionState.RECONNECTING
    attempted = False

    async def connect(_request_authorization=False):
        nonlocal attempted
        attempted = True

    provider.connect = connect
    await provider.apply(AvatarState(mouth_open=.4, expression="happy"))
    assert attempted is False


@pytest.mark.asyncio
async def test_vts_discovers_real_targets_lip_sync_coexists_and_model_switch_refreshes():
    provider = VTubeStudioAvatarProvider(VTubeStudioConfig(enabled=True, renderer="VTUBE_STUDIO"))
    client = FakeVTSClient(); provider.client = client
    await provider._refresh_model_metadata(force=True)

    assert provider._resolve_emotion_target("happy")["kind"] == "hotkey"
    assert provider._resolve_emotion_target("concerned")["kind"] == "expression"
    assert provider._resolve_emotion_target("amused")["kind"] == "parameter"
    assert provider._resolve_emotion_target("warning")["kind"] == "neutral"

    happy = await provider.apply_emotion("happy", .4, {"cooldown_ms": 250})
    assert happy["applied"] and happy["target"] == "NYRA_HAPPY"
    await provider.apply(AvatarState(mouth_open=.51, expression="happy"))
    assert provider.current_emotion == "happy"
    injection = [data for kind, data in client.calls if kind == "InjectParameterDataRequest"][-1]
    assert injection["parameterValues"] == [{"id": "MouthOpen", "value": .51, "weight": 1}]

    await provider.disconnect()
    pending = await provider.apply_emotion("concerned", .42, {})
    assert pending["kind"] == "offline" and provider.current_emotion == "concerned"
    provider.state = VTSConnectionState.RECONNECTING; client.socket = object()
    await provider._refresh_model_metadata(force=True)
    assert provider.last_emotion_presentation["emotion"] == "concerned"
    assert provider.last_emotion_presentation["applied"] is True

    client.model_id = "model-2"; client.with_capabilities = False
    await provider._refresh_model_metadata()
    assert provider.model["modelID"] == "model-2"
    assert provider.hotkeys == [] and provider.expressions == []
    assert provider.last_emotion_presentation["fallback"] == "model_has_no_compatible_emotion_capability"
    await provider.apply_emotion("neutral", .35, {"cooldown_ms": 500})
    missing = await provider.apply_emotion("happy", .35, {"cooldown_ms": 500})
    assert missing["emotion"] == "happy" and missing["applied"] is False
    assert missing["fallback"] == "model_has_no_compatible_emotion_capability"


def test_configured_vts_map_is_validated_against_discovered_capabilities():
    provider = VTubeStudioAvatarProvider(VTubeStudioConfig(
        emotion_map={"warning": VTSEmotionBinding(kind="hotkey", target="SAFE_WARNING")},
    ))
    provider.hotkeys = [{"hotkeyID": "warning-id", "name": "SAFE_WARNING", "type": "ToggleExpression"}]
    assert provider._resolve_emotion_target("warning")["id"] == "warning-id"
    provider.hotkeys = []
    assert provider._resolve_emotion_target("warning") == {"kind": "neutral", "target": None}
