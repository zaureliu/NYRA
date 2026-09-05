from __future__ import annotations

from pathlib import Path

import pytest

from app.character.state import EmotionalState
from app.api.routes import SAFE_AUDIO
from app.speech.emotion import EmotionPlan, EmotionPlanner, KAZUMI_VOICE, VALID_EMOTIONS
from app.speech.profile import VoiceSynthesisOptions, load_voice_profile
from app.speech.prosody import SpeechTextNormalizer
from app.speech.queue import SpeechQueue
from app.speech.tts import (
    KokoroTTSProvider,
    Pyttsx3TTS,
    SpeechCache,
    TTSProvider,
)


EXPECTED_EMOTIONS = {
    "neutral", "friendly", "focused", "confident", "positive", "happy",
    "relieved", "concerned", "warning", "serious", "empathetic", "curious",
    "surprised", "amused", "apologetic", "uncertain", "calm",
}


def test_voice_identity_and_emotion_allowlist_are_stable() -> None:
    assert VALID_EMOTIONS == EXPECTED_EMOTIONS
    assert KAZUMI_VOICE.identity_id == "KAZUMI_VOICE_AVA_V1"
    assert KAZUMI_VOICE.language == "pt-BR"
    assert KAZUMI_VOICE.presentation == "adult_young_feminine"
    assert EXPECTED_EMOTIONS <= {state.value for state in EmotionalState}


def test_invalid_emotion_is_neutral_and_intensity_is_bounded() -> None:
    unknown = EmotionPlan.validated("invented", 9)
    invalid_intensity = EmotionPlan.validated("happy", "invalid")  # type: ignore[arg-type]
    assert unknown.emotion.value == "neutral"
    assert unknown.intensity == 0.65
    assert invalid_intensity.intensity == 0.2


@pytest.mark.parametrize(
    ("response", "context", "expected"),
    [
        ("Estou verificando o serviço agora.", {"technical": True}, {"focused", "curious"}),
        ("Pronto. A operação foi concluída.", {"outcome": "completed"}, {"confident"}),
        ("Certo... finalmente voltou ao normal.", {"outcome": "recovered"}, {"relieved"}),
        ("O serviço falhou novamente.", {"outcome": "failed"}, {"concerned"}),
        ("Essa operação vai desligar a máquina virtual.", {"destructive": True}, {"warning"}),
        ("Ué... ele voltou sozinho.", {"unexpected": True}, {"surprised"}),
        ("Desculpa. Eu interpretei esse comando errado.", {"kazumi_error": True}, {"apologetic"}),
    ],
)
def test_auto_emotion_selection_uses_semantics_and_structured_context(
    response: str, context: dict[str, object], expected: set[str]
) -> None:
    plan = EmotionPlanner().plan("Verifique isso.", response, context=context, turn_id="turn-test", sentence_index=0)
    assert plan.emotion.value in expected
    assert 0 <= plan.intensity <= 0.65
    assert plan.style_instruction


def test_emotion_continuity_is_sentence_scoped_not_cross_turn() -> None:
    planner = EmotionPlanner()
    first = planner.plan("Veja isso", "Encontrei um erro.", turn_id="turn-a", sentence_index=0)
    held = planner.plan("Veja isso", "Pode ser o DNS.", turn_id="turn-a", sentence_index=1)
    next_turn = planner.plan("Funcionou?", "Pronto. Funcionou!", turn_id="turn-b", sentence_index=0)
    assert first.emotion.value == "concerned"
    assert held.emotion.value == "concerned"
    assert next_turn.emotion.value in {"happy", "positive"}


def test_expressiveness_is_progressive_and_neutral_only_is_acoustically_safe() -> None:
    low = EmotionPlanner(expressiveness="low").plan("", "Funcionou!", turn_id="low")
    normal = EmotionPlanner(expressiveness="normal").plan("", "Funcionou!", turn_id="normal")
    high = EmotionPlanner(expressiveness="high").plan("", "Funcionou!", turn_id="high")
    neutral = EmotionPlanner(emotion_mode="neutral_only").plan("", "Funcionou!", turn_id="neutral")
    assert low.intensity < normal.intensity < high.intensity <= 0.65
    assert neutral.emotion.value == "neutral"
    assert neutral.intensity == 0


def test_emotion_metadata_markdown_json_and_internal_trace_are_never_spoken() -> None:
    normalizer = SpeechTextNormalizer()
    prepared = normalizer.prepare(
        "<emotion=concerned intensity=0.3> **Atenção.**\nTRACE_INTERNAL=secret"
    )
    assert "emotion" not in prepared.speech_text.casefold()
    assert "trace" not in prepared.speech_text.casefold()
    assert "**" not in prepared.speech_text
    assert "Atenção" in prepared.speech_text
    structured = normalizer.prepare('{"emotion":"happy","trace":"private"}')
    assert "happy" not in structured.speech_text


def test_ptbr_technical_normalization_covers_units_time_addresses_and_lexicon() -> None:
    prepared = SpeechTextNormalizer().prepare(
        "KAZUMI viu Proxmark3 na VM 120 com 32 GB, RX 7600, às 20:45. "
        "O endereço é 192.168.1.2 e retornou HTTP 422."
    )
    speech = prepared.speech_text
    assert "Kazumi" in speech and "Naira" not in speech
    assert "Próxmark três" in speech
    assert "trinta e dois gigabytes" in speech
    assert "vinte horas e quarenta e cinco minutos" in speech
    assert "cento e noventa e dois" in speech
    assert "quatrocentos e vinte e dois" in speech


def test_voice_profile_has_every_emotion_and_no_pitch_solution() -> None:
    raw, options = load_voice_profile()
    assert raw["profile_id"] == "KAZUMI_VOICE_AVA_V1"
    assert options.voice == "en-US-AvaMultilingualNeural"
    assert raw["selection"]["pitch_shift"] is False
    assert raw["selection"]["native_emotion_support"] is False
    assert EXPECTED_EMOTIONS <= set(raw["emotion_modifiers"])
    assert options.edge_pitch == "+0Hz"


def test_cache_key_separates_emotion_intensity_style_speed_and_voice() -> None:
    base = VoiceSynthesisOptions()
    happy = base.with_emotion(EmotionPlan.validated("happy", 0.4))
    concerned = base.with_emotion(EmotionPlan.validated("concerned", 0.4))
    assert happy.cache_key_data(engine="kokoro", model="m", text="Oi") != concerned.cache_key_data(
        engine="kokoro", model="m", text="Oi"
    )
    profile, _defaults = load_voice_profile()
    assert happy.for_state("neutral", profile).emotion == "happy"


def test_runtime_cache_keeps_consumer_file_and_removes_producer_copy(tmp_path: Path) -> None:
    cache = SpeechCache(tmp_path / "cache", max_entries=2)
    source = tmp_path / "generated.wav"
    source.write_bytes(b"RIFF" + b"x" * 200)
    payload = VoiceSynthesisOptions().cache_key_data(engine="test", model="m", text="Olá")
    cached = cache.put(payload, source)
    assert cached.is_absolute() and cached.is_file()
    assert SAFE_AUDIO.fullmatch(cached.name)
    assert not source.exists()
    assert cache.get(payload) == cached


def test_kokoro_reports_capabilities_honestly(tmp_path: Path) -> None:
    provider = KokoroTTSProvider(tmp_path / "model", tmp_path / "voices")
    assert provider.capabilities().supports_emotion is False
    assert provider.capabilities().supports_native_speed is True


def test_kokoro_fallback_never_mixes_speaker_embeddings(tmp_path: Path) -> None:
    provider = KokoroTTSProvider(tmp_path / "model", tmp_path / "voices")
    assert provider._resolve_voice(object(), "kazumi_voice_v2") == "pf_dora"
    assert provider.default_voice == "pf_dora"
    assert provider.describe()["custom_style"] is False


def test_sapi_worker_uses_packaged_dispatch_in_frozen_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    command = Pyttsx3TTS._command(["--probe"])
    assert command[1:] == ["--sapi-worker", "--probe"]


@pytest.mark.asyncio
async def test_speech_queue_preserves_turn_and_emotion_options(tmp_path: Path) -> None:
    class CapturingProvider(TTSProvider):
        def __init__(self) -> None:
            self.received: VoiceSynthesisOptions | None = None

        @property
        def name(self) -> str:
            return "capture"

        async def health(self) -> bool:
            return True

        async def synthesize(self, text: str, state: str = "neutral", options=None) -> Path:
            self.received = options
            output = tmp_path / "voice.wav"
            output.write_bytes(b"RIFF" + b"x" * 200)
            return output.resolve()

    queue = SpeechQueue()
    provider = CapturingProvider()
    options = VoiceSynthesisOptions().with_emotion(EmotionPlan.validated("warning", 0.5))
    output = await queue.synthesize(
        provider, "Atenção.", "warning", response_id="response", turn_id="turn_response", options=options
    )
    await queue.stop()
    assert output.is_file()
    assert provider.received is not None
    assert provider.received.emotion == "warning"
    assert provider.received.emotion_intensity == 0.5
