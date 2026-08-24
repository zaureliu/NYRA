from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.paths import DATA_ROOT, IDENTITY_ROOT, LOG_ROOT
from app.speech.profile import VoiceSynthesisOptions, load_voice_profile
from app.speech.prosody import ProsodyProcessor
from app.speech.reference import REFERENCE_PATH

from .audio import analyze_audio, normalize_candidate_audio, sha256_file
from .catalog import curated_candidates
from .models import (
    CandidateStatus,
    HunterPhase,
    LatencyMetrics,
    STTValidation,
    VoiceCandidate,
    VoiceHunterState,
)

BENCHMARK_PHRASES = {
    "casual": "Oi... eu sou a Nyra. Acho que agora estamos chegando mais perto da minha voz.",
    "natural": "Eu estava olhando a rede enquanto você trabalhava. Por enquanto, tá tudo tranquilo.",
    "curiosa": "Hmm... apareceu alguma coisa diferente aqui. Quer que eu veja o que é?",
    "humor_seco": "DNS funcionando normalmente. Quase suspeito.",
    "tecnica": "O Proxmox está online, o Docker continua respondendo e o OpenWrt não apresentou nenhum alerta.",
    "rede": "A latência está em vinte e três milissegundos, sem perda de pacotes e com o gateway respondendo normalmente.",
    "alerta": "Aurélio... o Sentinel acabou de perder comunicação com um dos nós.",
    "long_form": (
        "Eu terminei uma leitura mais longa da rede. O Proxmox segue online, o Docker continua respondendo, "
        "e o OpenWrt não registrou nenhuma mudança importante. A latência média ficou em vinte e três "
        "milissegundos, sem perda de pacotes. O DNS, o SSH, o Nginx e o Cloudflare também responderam como "
        "esperado. Por enquanto, não há motivo para interromper o que você está fazendo. Vou continuar "
        "observando em silêncio e aviso se o Sentinel encontrar algo que realmente mereça atenção."
    ),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class VoiceHunterService:
    def __init__(
        self,
        stt: Any = None,
        tts_catalog: list[Any] | None = None,
        root: Path | None = None,
        download_budget_bytes: int | None = None,
    ) -> None:
        self.root = root or DATA_ROOT / "voices" / "candidates"
        self.index_path = self.root.parent / "voice-hunter-index.json"
        self.state_path = self.root.parent / "voice-hunter-state.json"
        self.search_log = LOG_ROOT / "voice-hunter-search.jsonl"
        configured_gb = float(os.getenv("MAX_VOICE_HUNTER_DOWNLOAD_GB", "8"))
        self.budget = download_budget_bytes or round(configured_gb * 1024**3)
        self.stt = stt
        self.tts_catalog = tts_catalog or []
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.search_log.parent.mkdir(parents=True, exist_ok=True)
        self.candidates = self._load_candidates()
        self.state = self._load_state()

    def _load_candidates(self) -> list[VoiceCandidate]:
        fresh = {item.id: item for item in curated_candidates()}
        if self.index_path.is_file():
            try:
                cached = [VoiceCandidate.model_validate(item) for item in json.loads(self.index_path.read_text(encoding="utf-8"))]
                for previous in cached:
                    if previous.id not in fresh:
                        continue
                    current = fresh[previous.id]
                    for field in ("favorite", "discarded", "my_rating", "sample_file", "provenance", "analysis", "stt", "latency", "benchmark"):
                        value = getattr(previous, field)
                        if value not in (None, {}, False):
                            setattr(current, field, value)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        for candidate in fresh.values():
            sample = self.root / candidate.id / "sample.wav"
            if sample.is_file():
                try:
                    stored_sample = sample.relative_to(DATA_ROOT)
                except ValueError:
                    stored_sample = sample
                candidate.sample_file = str(stored_sample).replace("\\", "/")
                candidate.provenance.setdefault("sha256", sha256_file(sample))
                if candidate.analysis is None:
                    candidate.analysis = analyze_audio(sample)
        return list(fresh.values())

    def _load_state(self) -> VoiceHunterState:
        if self.state_path.is_file():
            try:
                state = VoiceHunterState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
                if state.phase not in {HunterPhase.READY, HunterPhase.IDLE}:
                    state.phase = HunterPhase.IDLE
                    state.message = "Pesquisa anterior interrompida; cache preservado."
                state.download_budget_bytes = self.budget
                return state
            except (OSError, ValueError):
                pass
        return VoiceHunterState(download_budget_bytes=self.budget, candidate_count=len(self.candidates))

    async def _persist(self) -> None:
        async with self._write_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            candidate_text = json.dumps([item.model_dump(mode="json") for item in self.candidates], ensure_ascii=False, indent=2) + "\n"
            state_text = self.state.model_dump_json(indent=2) + "\n"
            await asyncio.to_thread(self.index_path.write_text, candidate_text, "utf-8")
            await asyncio.to_thread(self.state_path.write_text, state_text, "utf-8")
            for candidate in self.candidates:
                directory = self.root / candidate.id
                if (directory / "sample.wav").is_file():
                    await asyncio.to_thread(self._write_candidate_files, candidate, directory)

    @staticmethod
    def _write_candidate_files(candidate: VoiceCandidate, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "metadata.json").write_text(candidate.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (directory / "license.txt").write_text(
            f"{candidate.license}\n{candidate.license_url or candidate.source_url}\n\n{candidate.allowed_use}\n",
            encoding="utf-8",
        )
        (directory / "source.json").write_text(
            json.dumps(candidate.provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def list_candidates(self, include_rejected: bool = True) -> list[dict[str, Any]]:
        output = []
        for candidate in self.candidates:
            if not include_rejected and candidate.status == CandidateStatus.REJECTED:
                continue
            value = candidate.model_dump(mode="json")
            if candidate.sample_file and (DATA_ROOT / candidate.sample_file).is_file():
                value["sample_url"] = f"/api/voice-hunter/candidates/{candidate.id}/sample"
            output.append(value)
        return output

    def get_candidate(self, candidate_id: str) -> VoiceCandidate:
        candidate = next((item for item in self.candidates if item.id == candidate_id), None)
        if candidate is None:
            raise KeyError(candidate_id)
        return candidate

    def sample_path(self, candidate_id: str) -> Path:
        candidate = self.get_candidate(candidate_id)
        path = self.root / candidate.id / "sample.wav"
        if not path.is_file():
            raise FileNotFoundError(candidate_id)
        return path

    async def start_search(self) -> VoiceHunterState:
        if self._task and not self._task.done():
            return self.state
        self._cancel = asyncio.Event()
        self._task = asyncio.create_task(self._run_search(), name="nyra-voice-hunter")
        await asyncio.sleep(0)
        return self.state

    async def cancel(self) -> VoiceHunterState:
        self._cancel.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state.phase = HunterPhase.IDLE
        self.state.cancelled = True
        self.state.finished_at = utc_now()
        self.state.message = "Pesquisa cancelada; resultados anteriores preservados."
        await self._persist()
        return self.state

    async def _run_search(self) -> None:
        self.state = VoiceHunterState(
            phase=HunterPhase.SEARCHING, progress=5, message="Consultando fontes primárias...",
            started_at=utc_now(), candidate_count=len(self.candidates), download_budget_bytes=self.budget,
            downloaded_bytes=self._downloaded_bytes(),
        )
        await self._persist()
        try:
            unique_urls = list(dict.fromkeys(str(item.source_url) for item in self.candidates))
            async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
                results = await asyncio.gather(*(self._verify_source(client, url) for url in unique_urls))
            if self._cancel.is_set():
                return
            self.state.phase = HunterPhase.CHECKING_LICENSES
            self.state.progress = 35
            self.state.message = "Aplicando classificação de licença e identidade..."
            for candidate in self.candidates:
                available = dict(results).get(str(candidate.source_url), False)
                await self._append_search_log(candidate, available)
            await self._persist()
            self.state.phase = HunterPhase.ANALYZING
            self.state.progress = 60
            self.state.message = "Analisando samples cacheados..."
            for candidate in self.candidates:
                if self._cancel.is_set():
                    return
                path = self.root / candidate.id / "sample.wav"
                if path.is_file():
                    candidate.analysis = await asyncio.to_thread(analyze_audio, path)
                    candidate.provenance["sha256"] = await asyncio.to_thread(sha256_file, path)
            self.state.phase = HunterPhase.BENCHMARKING
            self.state.progress = 82
            self.state.message = "Validando transcrições disponíveis..."
            await self._validate_cached_stt()
            self.state.phase = HunterPhase.READY
            self.state.progress = 100
            self.state.message = "Candidatas prontas para audição e comparação."
            self.state.finished_at = utc_now()
            self.state.candidate_count = len(self.candidates)
            self.state.downloaded_bytes = self._downloaded_bytes()
            await self._persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.phase = HunterPhase.ERROR
            self.state.error = type(exc).__name__
            self.state.message = "A busca online falhou; o cache anterior continua disponível."
            self.state.finished_at = utc_now()
            await self._persist()

    @staticmethod
    async def _verify_source(client: httpx.AsyncClient, url: str) -> tuple[str, bool]:
        try:
            response = await client.head(url)
            if response.status_code in {403, 405}:
                response = await client.get(url, headers={"Range": "bytes=0-1024"})
            return url, response.status_code < 400
        except httpx.HTTPError:
            return url, False

    async def _append_search_log(self, candidate: VoiceCandidate, source_available: bool) -> None:
        entry = {
            "at": utc_now(), "source": candidate.source, "url": str(candidate.source_url),
            "candidate": candidate.id, "license": candidate.license,
            "decision": candidate.status.value, "source_available": source_available,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        await asyncio.to_thread(self._append_text, self.search_log, line)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    async def register_sample(self, candidate_id: str, source: Path, downloaded_from: str | None = None) -> VoiceCandidate:
        candidate = self.get_candidate(candidate_id)
        if candidate.status == CandidateStatus.REJECTED:
            raise PermissionError("candidata rejeitada não pode ser baixada")
        incoming = source.stat().st_size
        current = self._downloaded_bytes()
        if current + incoming > self.budget:
            raise OverflowError("MAX_VOICE_HUNTER_DOWNLOAD_GB excedido")
        directory = self.root / candidate.id
        destination = directory / "sample.wav"
        analysis = await asyncio.to_thread(normalize_candidate_audio, source, destination)
        digest = await asyncio.to_thread(sha256_file, destination)
        for other in self.candidates:
            if other.id != candidate.id and other.provenance.get("sha256") == digest:
                destination.unlink(missing_ok=True)
                raise FileExistsError(f"sample duplicado de {other.id}")
        try:
            stored_path = destination.relative_to(DATA_ROOT)
        except ValueError:
            stored_path = destination
        candidate.sample_file = str(stored_path).replace("\\", "/")
        candidate.analysis = analysis
        candidate.provenance.update({
            "source": downloaded_from or str(candidate.source_url), "downloaded_at": utc_now(),
            "license": candidate.license, "sha256": digest, "usage": candidate.status.value,
        })
        await self._persist()
        return candidate

    async def _validate_cached_stt(self) -> None:
        if self.stt is None or not await self.stt.health():
            return
        for candidate in self.candidates:
            path = self.root / candidate.id / "sample.wav"
            if not path.is_file() or candidate.stt is not None:
                continue
            try:
                result = await self.stt.transcribe(path)
                candidate.stt = STTValidation(
                    language=result.language, confidence=result.language_probability,
                    transcription=result.text, duration_s=result.duration_seconds,
                )
            except Exception:
                continue

    async def preview(self, candidate_id: str, phrase: str = "casual", text: str | None = None) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate.status == CandidateStatus.REJECTED:
            raise PermissionError("candidata rejeitada")
        requested = text or BENCHMARK_PHRASES.get(phrase, BENCHMARK_PHRASES["casual"])
        if len(requested) > 4000:
            raise ValueError("texto excede 4000 caracteres")
        provider = next((item for item in self.tts_catalog if item.name == candidate.provider), None)
        if candidate.status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE:
            provider = next((item for item in self.tts_catalog if item.name == "chatterbox_multilingual_v3"), None)
        if provider is None or not await provider.health():
            path = self.sample_path(candidate_id)
            return {"candidate_id": candidate_id, "audio_url": f"/api/voice-hunter/candidates/{candidate_id}/sample", "cached": True}
        prepared = ProsodyProcessor().prepare(requested, provider=provider.name)
        options = VoiceSynthesisOptions(provider=provider.name, voice=candidate.provider_voice or "default")
        started = time.perf_counter()
        if candidate.status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE and hasattr(provider, "synthesize_with_reference"):
            output = await provider.synthesize_with_reference(prepared.speech_text, self.sample_path(candidate_id), "neutral", options)
        else:
            output = await provider.synthesize(prepared.speech_text, "neutral", options)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        analysis = await asyncio.to_thread(analyze_audio, output)
        candidate.latency = LatencyMetrics(
            total_synthesis_ms=elapsed_ms, warm_generation_ms=elapsed_ms,
            audio_duration_ms=round(analysis.duration_s * 1000, 1),
            real_time_factor=round(elapsed_ms / max(analysis.duration_s * 1000, 1), 3), measured_at=utc_now(),
        )
        await self._persist()
        return {
            "candidate_id": candidate_id, "audio_url": f"/api/audio/{output.name}", "cached": False,
            "speech_text": prepared.speech_text, "latency": candidate.latency.model_dump(mode="json"),
        }

    async def set_preference(self, candidate_id: str, *, favorite: bool | None = None, discarded: bool | None = None, rating: float | None = None) -> VoiceCandidate:
        candidate = self.get_candidate(candidate_id)
        if favorite is not None:
            candidate.favorite = favorite
        if discarded is not None:
            candidate.discarded = discarded
            if discarded:
                candidate.favorite = False
        if rating is not None:
            if not 0 <= rating <= 10:
                raise ValueError("rating deve estar entre 0 e 10")
            candidate.my_rating = rating
        await self._persist()
        return candidate

    async def select_official(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate.status not in {CandidateStatus.SAFE_FOR_NYRA_REFERENCE, CandidateStatus.SAFE_FOR_DIRECT_TTS}:
            raise PermissionError(f"{candidate.status.value} não pode ser voz oficial")
        profile_path = IDENTITY_ROOT / "voice_profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        backup_dir = DATA_ROOT / "voices" / "profile-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (backup_dir / f"voice-profile-{stamp}.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if candidate.status == CandidateStatus.SAFE_FOR_NYRA_REFERENCE:
            source = self.sample_path(candidate_id)
            REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, REFERENCE_PATH)
            profile.update({"provider": "chatterbox_multilingual_v3", "voice": "nyra_reference", "reference_file": "data/voices/nyra_reference.wav"})
        else:
            provider = next((item for item in self.tts_catalog if item.name == candidate.provider), None)
            if provider is None or not await provider.health():
                raise RuntimeError("provider direto não está disponível neste host")
            profile.update({"provider": candidate.provider, "voice": candidate.provider_voice or "default"})
        profile["profile_id"] = "NYRA_VOICE"
        profile["selected_by_user_at"] = utc_now()
        profile["voice_hunter_candidate_id"] = candidate.id
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"saved": True, "candidate_id": candidate.id, "provider": profile["provider"], "voice": profile["voice"]}

    async def cleanup_discarded(self) -> dict[str, Any]:
        removed: list[str] = []
        root = self.root.resolve()
        for candidate in self.candidates:
            if not candidate.discarded:
                continue
            directory = (self.root / candidate.id).resolve()
            if directory.parent != root or not directory.is_dir():
                continue
            shutil.rmtree(directory)
            candidate.sample_file = None
            candidate.analysis = None
            candidate.stt = None
            candidate.provenance = {}
            removed.append(candidate.id)
        await self._persist()
        return {"removed": removed, "bytes_remaining": self._downloaded_bytes()}

    def _downloaded_bytes(self) -> int:
        candidate_files = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        # OmniVoice was downloaded specifically for this hunt, but its Hugging Face cache
        # lives outside candidates/. Pre-existing Kokoro/Chatterbox installs are not charged.
        downloaded_models = sum(
            candidate.size_bytes or 0
            for candidate in self.candidates
            if candidate.provider == "omnivoice_brpt" and (self.root / candidate.id / "sample.wav").is_file()
        )
        return candidate_files + downloaded_models
