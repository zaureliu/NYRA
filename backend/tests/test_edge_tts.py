from pathlib import Path

import pytest

from app.speech.profile import VoiceSynthesisOptions
from app.speech.tts import EdgeTTSProvider


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
