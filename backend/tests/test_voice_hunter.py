from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes import router
from app.voice_hunter.audio import analyze_audio, duplicate_hashes, normalize_candidate_audio, sha256_file
from app.voice_hunter.models import CandidateStatus, VoiceCandidate
from app.voice_hunter.service import VoiceHunterService


def make_wav(path: Path, rate: int = 16000, frequency: float = 190.0) -> Path:
    active = 0.22 * np.sin(2 * np.pi * frequency * np.arange(rate * 2) / rate)
    samples = np.concatenate([np.zeros(rate // 10), active, np.zeros(rate // 10)]).astype(np.float32)
    sf.write(path, samples, rate, subtype="PCM_16")
    return path


def test_metadata_validation_and_license_invariant(tmp_path: Path):
    valid = VoiceHunterService(root=tmp_path / "candidates", download_budget_bytes=1024).candidates[0]
    assert valid.status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE
    payload = valid.model_dump(mode="json")
    payload["reference_allowed"] = False
    with pytest.raises(ValidationError):
        VoiceCandidate.model_validate(payload)


def test_catalog_license_classification(tmp_path: Path):
    service = VoiceHunterService(root=tmp_path / "candidates")
    by_id = {item.id: item for item in service.candidates}
    assert by_id["omnivoice-brpt-calm-design"].status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE
    assert by_id["kokoro-pf-dora"].status == CandidateStatus.SAFE_FOR_DIRECT_TTS
    assert by_id["common-voice-ptbr-female"].status == CandidateStatus.AUDITION_ONLY
    assert by_id["piper-ptbr-current"].status == CandidateStatus.REJECTED
    assert by_id["qwen3-tts-voice-design"].status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE


def test_sha256_and_duplicate_detection(tmp_path: Path):
    first = tmp_path / "a.bin"; second = tmp_path / "b.bin"
    first.write_bytes(b"nyra"); second.write_bytes(b"nyra")
    expected = hashlib.sha256(b"nyra").hexdigest()
    assert sha256_file(first) == expected
    assert duplicate_hashes([first, second]) == {expected: [first, second]}


def test_audio_preprocessing_is_pcm_mono_and_conservative(tmp_path: Path):
    source = make_wav(tmp_path / "source.wav")
    output = tmp_path / "normalized.wav"
    metrics = normalize_candidate_audio(source, output)
    info = sf.info(output)
    assert info.samplerate == 24000
    assert info.channels == 1
    assert info.subtype == "PCM_16"
    assert metrics.acceptable
    assert metrics.peak <= .9
    assert analyze_audio(output).clipping_ratio == 0


@pytest.mark.asyncio
async def test_download_budget_and_duplicate_registration(tmp_path: Path):
    source = make_wav(tmp_path / "source.wav")
    tiny = VoiceHunterService(root=tmp_path / "tiny" / "candidates", download_budget_bytes=100)
    with pytest.raises(OverflowError):
        await tiny.register_sample("kokoro-pf-dora", source)

    service = VoiceHunterService(root=tmp_path / "normal" / "candidates", download_budget_bytes=10_000_000)
    first = await service.register_sample("kokoro-pf-dora", source)
    assert first.provenance["sha256"]
    with pytest.raises(FileExistsError):
        await service.register_sample("chatterbox-multilingual-default", source)


@pytest.mark.asyncio
async def test_candidate_loading_favorites_and_cleanup_are_scoped(tmp_path: Path):
    root = tmp_path / "voices" / "candidates"
    service = VoiceHunterService(root=root, download_budget_bytes=10_000_000)
    source = make_wav(tmp_path / "source.wav")
    await service.register_sample("kokoro-pf-dora", source)
    await service.set_preference("kokoro-pf-dora", favorite=True, discarded=True, rating=8)
    official = tmp_path / "voices" / "nyra_reference.wav"
    official.write_bytes(b"official")

    reloaded = VoiceHunterService(root=root, download_budget_bytes=10_000_000)
    candidate = reloaded.get_candidate("kokoro-pf-dora")
    assert candidate.discarded is True and candidate.favorite is False and candidate.my_rating == 8
    result = await reloaded.cleanup_discarded()
    assert result["removed"] == ["kokoro-pf-dora"]
    assert official.read_bytes() == b"official"


@pytest.mark.asyncio
async def test_selection_safety_blocks_audition_and_rejected(tmp_path: Path):
    service = VoiceHunterService(root=tmp_path / "candidates")
    with pytest.raises(PermissionError):
        await service.select_official("edge-thalita-multilingual")
    with pytest.raises(PermissionError):
        await service.select_official("piper-ptbr-current")


@pytest.mark.asyncio
async def test_manual_search_uses_mocked_sources_and_reaches_ready(monkeypatch, tmp_path: Path):
    async def available(_client, url):
        return url, True
    monkeypatch.setattr(VoiceHunterService, "_verify_source", staticmethod(available))
    service = VoiceHunterService(root=tmp_path / "candidates")
    await service.start_search()
    assert service._task is not None
    await service._task
    assert service.state.phase.value == "READY"
    assert service.state.candidate_count == 12


def test_voice_hunter_api_loads_candidates(tmp_path: Path):
    service = VoiceHunterService(root=tmp_path / "candidates")
    app = FastAPI()
    app.state.services = SimpleNamespace(voice_hunter=service)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/voice-hunter/status")
        assert response.status_code == 200
        assert len(response.json()["candidates"]) == 12
        blocked = client.post("/api/voice-hunter/candidates/edge-thalita-multilingual/select")
        assert blocked.status_code == 403
