from pathlib import Path

import pytest

from app.speech.profile import VoiceSynthesisOptions
from app.speech.tts import EdgeTTSProvider
from app.speech.tts_identity import NYRA_VOICE_ID


def test_edge_options_accept_safe_ranges():
    options = VoiceSynthesisOptions(provider="edge_tts", edge_rate="-8%", edge_pitch="-2Hz", edge_volume="+0%")
    assert options.edge_rate == "-8%"
    with pytest.raises(ValueError):
        VoiceSynthesisOptions(provider="edge_tts", edge_rate="-80%")


@pytest.mark.asyncio
async def test_edge_inventory_filters_ptbr_female(monkeypatch, tmp_path):
    async def fake_list():
        return [
            {"ShortName": "pt-BR-A", "FriendlyName": "A", "Locale": "pt-BR", "Gender": "Female"},
            {"ShortName": "pt-BR-M", "FriendlyName": "M", "Locale": "pt-BR", "Gender": "Male"},
            {"ShortName": "en-US-F", "FriendlyName": "F", "Locale": "en-US", "Gender": "Female"},
        ]
    import edge_tts
    monkeypatch.setattr(edge_tts, "list_voices", fake_list)
    EdgeTTSProvider._voice_cache = None
    provider = EdgeTTSProvider()
    provider.cache_path = tmp_path / "edge-voices.json"
    voices = await provider.refresh_voices()
    assert [item["id"] for item in voices] == ["pt-BR-A"]


@pytest.mark.asyncio
async def test_edge_synthesis_never_substitutes_approved_ava(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    class FakeCommunicate:
        def __init__(self, _text, voice, **_options):
            captured["voice"] = voice

        async def save(self, path):
            Path(path).write_bytes(b"mp3")

    import edge_tts
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    provider = EdgeTTSProvider(voice=NYRA_VOICE_ID)
    provider.output_dir = tmp_path
    monkeypatch.setattr(
        provider,
        "_decode_mp3",
        lambda _source, destination: destination.write_bytes(b"RIFF" + b"x" * 200),
    )
    output = await provider.synthesize(
        "Oi. Eu sou a Nyra.",
        options=VoiceSynthesisOptions(provider="edge_tts", voice="pt-BR-OtherNeural"),
    )
    assert output.is_file()
    assert captured["voice"] == "en-US-AvaMultilingualNeural"
    assert provider.engine_id == "edge_neural"
