from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.character.response_style import apply_response_style
from app.speech.prosody import PronunciationDictionary, ProsodyProcessor
from app.speech.profile import VoiceSynthesisOptions, load_voice_profile
from app.speech.tts import ChatterboxTTSProvider, FallbackTTSProvider, KokoroTTSProvider, TTSProvider, create_tts_provider
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
    source = "NYRA verificou DNS, Proxmox e OpenWrt."
    prepared = ProsodyProcessor(PronunciationDictionary()).prepare(source)
    assert prepared.display_text == source
    assert "Naira" in prepared.speech_text
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
    assert raw["profile_id"] == "NYRA_VOICE"
    assert options.provider == "kokoro"
    assert options.voice == "pf_dora"
    assert "expressiveness" not in raw
    assert "provider_parameters" not in raw


def test_prosody_preserves_questions_and_all_technical_pronunciations():
    source = "NYRA usa Utamo, Proxmox, OpenWrt, Ollama, Qwen, Linux, Docker, Nginx, Cloudflare, VLAN, DNS, DHCP, SSH, HTTP, HTTPS, API, CPU, GPU, RAM, Wi-Fi, IPv4 e IPv6?"
    prepared = ProsodyProcessor().prepare(source)
    assert prepared.display_text == source
    assert prepared.speech_text.endswith("?")
    assert "Naira" in prepared.speech_text
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
    assert isinstance(selected, KokoroTTSProvider)


@pytest.mark.asyncio
async def test_runtime_tts_fallback(tmp_path: Path):
    class Broken(TTSProvider):
        name = "broken"
        async def health(self): return True
        async def synthesize(self, text, state="neutral", options=None): raise RuntimeError("boom")
    class Working(TTSProvider):
        name = "working"
        async def health(self): return True
        async def synthesize(self, text, state="neutral", options=None):
            output = tmp_path / "fallback.wav"; output.write_bytes(b"RIFFfallback"); return output
    output = await FallbackTTSProvider(Broken(), Working()).synthesize("teste")
    assert output.read_bytes().startswith(b"RIFF")
