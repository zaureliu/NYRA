from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.character.response_style import apply_response_style
from app.speech.prosody import PronunciationDictionary, ProsodyProcessor
from app.speech.profile import VoiceSynthesisOptions, load_voice_profile
from app.speech.tts import ChatterboxTTSProvider, EdgeTTSProvider, FallbackTTSProvider, KokoroTTSProvider, TTSProvider, create_tts_provider
from app.speech.vad import VADConfig


def test_prosody_separates_display_and_spoken_text():
    visual = "## Estado\nCPU: 17% | RAM: 62% | Uptime: 3d 14h 😊"
    prepared = ProsodyProcessor().prepare(visual)
    assert prepared.display_text == visual
    assert "##" not in prepared.speech_text
    assert "😊" not in prepared.speech_text
    assert "dezessete por cento" in prepared.speech_text
    assert "três dias e catorze horas" in prepared.speech_text
    assert all(len(chunk) <= 280 for chunk in prepared.chunks)


def test_response_style_removes_chatbot_filler_without_touching_facts():
    result = apply_response_style("Claro! CPU em 17%. 😊 Se precisar de algo, estou aqui para ajudar. O que deseja verificar ou testar?")
    assert result == "CPU em 17%."


def test_response_style_removes_attendant_closer_variants():
    result = apply_response_style("Sim, estou online. Se quiser, posso ajudar a verificar os serviços. O que deseja testar ou verificar?")
    assert result == "Sim, estou online."


def test_pronunciation_dictionary_changes_only_speech():
    source = "KAZUMI verificou DNS, Proxmox e OpenWrt."
    prepared = ProsodyProcessor(PronunciationDictionary()).prepare(source)
    assert prepared.display_text == source
    assert "Kazumi" in prepared.speech_text and "Naira" not in prepared.speech_text
    assert "dê ene ésse" in prepared.speech_text


def test_emotional_state_changes_voice_subtly():
    profile = {"emotion_modifiers": {"concerned": {"rate_multiplier": 0.94, "exaggeration_delta": -0.06, "cfg_delta": 0.04, "pause_multiplier": 1.12}}}
    base = VoiceSynthesisOptions()
    concerned = base.for_state("concerned", profile)
    assert concerned.speaking_rate < base.speaking_rate
    assert concerned.exaggeration < base.exaggeration
    assert concerned.cfg_weight > base.cfg_weight
    assert concerned.sentence_pause_ms > base.sentence_pause_ms


def test_official_voice_profile_contains_only_supported_parameters():
    raw, options = load_voice_profile()
    assert raw["profile_id"] == "KAZUMI_VOICE_AVA_V1"
    assert options.provider == "edge_tts"
    assert options.voice == "en-US-AvaMultilingualNeural"
    assert "expressiveness" not in raw
    assert "provider_parameters" not in raw


def test_prosody_preserves_questions_and_all_technical_pronunciations():
    source = "KAZUMI usa Utamo, Proxmox, OpenWrt, Ollama, Qwen, Linux, Docker, Nginx, Cloudflare, VLAN, DNS, DHCP, SSH, HTTP, HTTPS, API, CPU, GPU, RAM, Wi-Fi, IPv4 e IPv6?"
    prepared = ProsodyProcessor().prepare(source)
    assert prepared.display_text == source
    assert prepared.speech_text.endswith("?")
    assert "Kazumi" in prepared.speech_text and "Naira" not in prepared.speech_text
    assert "IP versão seis" in prepared.speech_text


def test_vad_and_device_settings_validate():
    vad = VADConfig(threshold=.58, min_speech_ms=300, min_silence_ms=700, speech_pad_ms=250)
    assert vad.threshold == .58
    settings = Settings.from_sources(microphone="usb-mic", speaker="usb-dac", desktop_click_through=True, desktop_overlay_scale=1.2)
    assert settings.microphone == "usb-mic"
    assert settings.speaker == "usb-dac"
    assert settings.desktop_click_through is True
    with pytest.raises(ValidationError):
        VADConfig(threshold=1.5)


def test_legacy_voice_default_migrates_to_approved_ava(monkeypatch):
    monkeypatch.setattr(
        "app.core.config._yaml_defaults",
        lambda: {"tts_provider": "kokoro", "tts_voice": "pf_dora"},
    )
    settings = Settings.from_sources()
    assert settings.tts_provider == "edge_tts"
    assert settings.tts_voice == "en-US-AvaMultilingualNeural"
    assert settings.tts_voice_identity_version == "ava-v1"


@pytest.mark.asyncio
async def test_edge_primary_retained_when_startup_is_offline(tmp_path: Path):
    selected = await create_tts_provider(
        "edge_tts",
        "pt-BR",
        tmp_path / "model",
        tmp_path / "voices",
        voice="en-US-AvaMultilingualNeural",
    )
    assert isinstance(selected, FallbackTTSProvider)
    assert isinstance(selected.primary, EdgeTTSProvider)
    assert selected.engine_id == "edge_neural"
    assert selected.active_voice == "en-US-AvaMultilingualNeural"
    assert isinstance(selected.fallback, FallbackTTSProvider)
    assert selected.fallback.primary.default_voice == "pf_dora"


@pytest.mark.asyncio
async def test_tts_provider_selection_keeps_kokoro(monkeypatch, tmp_path: Path):
    async def available(_self): return True
    monkeypatch.setattr(KokoroTTSProvider, "health", available)
    selected = await create_tts_provider("kokoro", "pt-BR", tmp_path / "model", tmp_path / "voices")
    assert isinstance(selected, FallbackTTSProvider)
    assert isinstance(selected.primary, KokoroTTSProvider)


@pytest.mark.asyncio
async def test_chatterbox_falls_back_to_kokoro(monkeypatch, tmp_path: Path):
    async def unavailable(_self): return False
    async def available(_self): return True
    monkeypatch.setattr(ChatterboxTTSProvider, "health", unavailable)
    monkeypatch.setattr(KokoroTTSProvider, "health", available)
    selected = await create_tts_provider("chatterbox", "pt-BR", tmp_path / "model", tmp_path / "voices", chatterbox_python=tmp_path / "python.exe")
    assert isinstance(selected, FallbackTTSProvider)
    assert isinstance(selected.primary, KokoroTTSProvider)


@pytest.mark.asyncio
async def test_runtime_tts_fallback(tmp_path: Path):
    class Broken(TTSProvider):
        name = "broken"
        attempts = 0
        @property
        def default_voice(self): return "primary_voice"
        async def health(self): return True
        async def synthesize(self, text, state="neutral", options=None):
            self.attempts += 1
            if self.attempts == 1: raise RuntimeError("boom")
            output = tmp_path / "primary.wav"; output.write_bytes(b"RIFFprimary"); return output
    class Working(TTSProvider):
        name = "working"
        @property
        def default_voice(self): return "fallback_voice"
        async def health(self): return True
        async def synthesize(self, text, state="neutral", options=None):
            output = tmp_path / "fallback.wav"; output.write_bytes(b"RIFFfallback"); return output
    provider = FallbackTTSProvider(Broken(), Working())
    output = await provider.synthesize("teste")
    assert output.read_bytes().startswith(b"RIFF")
    assert provider.fallback_active is True
    assert provider.active_engine == "working"
    assert provider.active_voice == "fallback_voice"
    assert provider.fallback_reason == "RuntimeError"
    recovered = await provider.synthesize("teste novamente")
    assert recovered.name == "primary.wav"
    assert provider.fallback_active is False
    assert provider.active_engine == "broken"
    assert provider.active_voice == "primary_voice"
