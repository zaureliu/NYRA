from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from uuid import uuid4

import numpy as np

from app.core.paths import BACKEND_ROOT, DATA_ROOT, IDENTITY_ROOT, PROJECT_ROOT
from app.speech.profile import VoiceSynthesisOptions, load_voice_profile
from app.speech.tts_identity import (
    KAZUMI_KOKORO_FALLBACK_VOICE,
    KAZUMI_PRIMARY_ENGINE,
    KAZUMI_VOICE_ID,
)


logger = logging.getLogger("kazumi.voice")

KAZUMI_EDGE_VOICE_ID = KAZUMI_VOICE_ID
KAZUMI_KOKORO_VOICE_ID = KAZUMI_KOKORO_FALLBACK_VOICE


@dataclass(frozen=True, slots=True)
class TtsCapabilities:
    supports_nonverbal: bool = False
    supports_cancellation: bool = True
    timestamps: bool = False
    phonemes: bool = False
    supports_emotion: bool = False
    supports_styles: bool = False
    supports_streaming: bool = False
    supports_reference_style: bool = False
    supports_native_speed: bool = False
    supports_native_pitch: bool = False
    style_instructions: bool = False
    voice_selection: bool = False
    voice_cloning: bool = False
    pt_br: bool = False
    offline: bool = True
    model_selection: bool = False
    pcm: bool = False
    wav: bool = True
    runtime_switch: bool = True

    def public_dict(self) -> dict[str, bool]:
        """Provider-neutral capability names used by Settings/diagnostics."""
        return {
            "nonverbal": self.supports_nonverbal,
            "cancellation": self.supports_cancellation,
            "timestamps": self.timestamps,
            "phonemes": self.phonemes,
            "streaming": self.supports_streaming,
            "emotion": self.supports_emotion,
            "style": self.supports_styles,
            "speed": self.supports_native_speed,
            "pitch": self.supports_native_pitch,
            "style_instructions": self.style_instructions,
            "voice_selection": self.voice_selection,
            "voice_cloning": self.voice_cloning,
            "pt_br": self.pt_br,
            "offline": self.offline,
            "model_selection": self.model_selection,
            "pcm": self.pcm,
            "wav": self.wav,
            "runtime_switch": self.runtime_switch,
        }


class SpeechCache:
    """Small bounded runtime cache whose key always includes emotion metadata."""

    def __init__(self, root: Path, max_entries: int = 256) -> None:
        self.root = root
        self.max_entries = max_entries
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, payload: dict[str, object]) -> Path:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.root / f"voice-cache-{hashlib.sha256(encoded).hexdigest()}.wav"

    def get(self, payload: dict[str, object]) -> Path | None:
        if len(str(payload.get("text") or "")) > 180:
            return None
        path = self.path_for(payload)
        if path.is_file() and path.stat().st_size > 100:
            try:
                path.touch()
            except OSError:
                pass
            return path.resolve()
        return None

    def put(self, payload: dict[str, object], source: Path) -> Path:
        if len(str(payload.get("text") or "")) > 180:
            return source.resolve()
        destination = self.path_for(payload)
        temporary = destination.with_suffix(".tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        if source.resolve() != destination.resolve():
            source.unlink(missing_ok=True)
        files = sorted(self.root.glob("voice-cache-*.wav"), key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in files[self.max_entries:]:
            stale.unlink(missing_ok=True)
        return destination.resolve()


class TTSProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def voices(self) -> list[dict[str, str]]:
        return []

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def provider_id(self) -> str:
        return self.name

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        return ()

    @property
    def provider_type(self) -> str:
        return "local"

    @property
    def default_voice(self) -> str:
        return "default"

    @property
    def engine_id(self) -> str:
        return self.name

    @property
    def active_engine(self) -> str:
        return self.engine_id

    @property
    def active_voice(self) -> str:
        return self.default_voice

    @property
    def fallback_active(self) -> bool:
        return False

    @property
    def fallback_reason(self) -> str | None:
        return None

    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities()

    def describe(self) -> dict[str, object]:
        return {
            "engine": self.name,
            "primary_engine": self.engine_id,
            "active_engine": self.active_engine,
            "voice": self.active_voice,
            "fallback_active": self.fallback_active,
            "fallback_reason": self.fallback_reason,
            "provider_type": self.provider_type,
            "model": getattr(self, "model_id", None),
            "capabilities": asdict(self.capabilities()),
        }

    async def list_voices(self, refresh: bool = False) -> list[dict[str, str]]:
        del refresh
        return list(self.voices)

    async def list_models(self, refresh: bool = False) -> list[dict[str, object]]:
        del refresh
        model = getattr(self, "model_id", None)
        return [{"id": model, "name": model}] if model else []

    def validate_configuration(self) -> tuple[bool, str | None]:
        return True, None

    async def cancel(self) -> None:
        """Cancel provider-owned work when supported.

        SpeechQueue cancellation already cancels the coroutine awaiting synthesize;
        network providers additionally override this hook to cancel their request tasks.
        """
        return None

    async def stream(
        self,
        text: str,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ):
        yield await self.synthesize(text, state, options)

    async def close(self) -> None:
        await self.cancel()

    async def stream_audio(self, text: str, state: str = "neutral", options=None):
        from app.speech.stream import AudioPacket
        yield AudioPacket(path=await self.synthesize(text, state, options))

    @abstractmethod
    async def health(self) -> bool: ...

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ) -> Path: ...


class DisabledTTS(TTSProvider):
    @property
    def name(self) -> str:
        return "disabled"

    async def health(self) -> bool:
        return False

    async def synthesize(
        self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None
    ) -> Path:
        raise RuntimeError("TTS desabilitado")


class FallbackTTSProvider(TTSProvider):
    """Keeps a healthy secondary provider for runtime synthesis failures."""

    def __init__(self, primary: TTSProvider, fallback: TTSProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_used_fallback = False
        self.last_failure_type: str | None = None

    @property
    def name(self) -> str:
        return self.primary.name

    @property
    def primary_name(self) -> str:
        return self.primary.name

    @property
    def fallback_name(self) -> str:
        return self.fallback.name

    @property
    def voices(self) -> list[dict[str, str]]:
        return self.primary.voices

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        return self.primary.supported_parameters

    @property
    def model_id(self) -> str | None:
        return getattr(self.primary, "model_id", None)

    @property
    def default_voice(self) -> str:
        return getattr(self.primary, "default_voice", "default")

    @property
    def engine_id(self) -> str:
        return self.primary.engine_id

    @property
    def active_engine(self) -> str:
        return self.fallback.active_engine if self.last_used_fallback else self.primary.active_engine

    @property
    def active_voice(self) -> str:
        return self.fallback.active_voice if self.last_used_fallback else self.primary.active_voice

    @property
    def fallback_active(self) -> bool:
        return self.last_used_fallback

    @property
    def fallback_reason(self) -> str | None:
        return self.last_failure_type if self.last_used_fallback else None

    def capabilities(self) -> TtsCapabilities:
        return self.primary.capabilities()

    def describe(self) -> dict[str, object]:
        value = self.primary.describe()
        value["fallback"] = self.fallback.describe()
        value["last_used_fallback"] = self.last_used_fallback
        value["last_failure_type"] = self.last_failure_type
        value["primary_engine"] = self.engine_id
        value["active_engine"] = self.active_engine
        value["voice"] = self.active_voice
        value["fallback_active"] = self.fallback_active
        value["fallback_reason"] = self.fallback_reason
        return value

    async def health(self) -> bool:
        return await self.primary.health() or await self.fallback.health()

    async def stream_audio(self, text: str, state: str = "neutral", options=None):
        emitted = False
        self.last_used_fallback = False
        try:
            async for packet in self.primary.stream_audio(text, state, options):
                emitted = True
                yield packet
        except Exception as exc:
            self.last_failure_type = type(exc).__name__
            if emitted:
                # Already-heard audio may not be replayed by a fallback.
                raise
            self.last_used_fallback = True
            fallback_options = options.model_copy(update={"provider": self.fallback.name,
                "voice": self.fallback.default_voice, "emotion": "neutral", "emotion_intensity": 0.0,
                "style_instruction": ""}) if options else None
            async for packet in self.fallback.stream_audio(text, "neutral", fallback_options):
                yield packet

    async def synthesize(self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None) -> Path:
        try:
            output = await self.primary.synthesize(text, state, options)
            self.last_used_fallback = False
            self.last_failure_type = None
            return output
        except Exception as exc:
            self.last_used_fallback = True
            self.last_failure_type = type(exc).__name__
            logger.warning("PRIMARY_TTS_FAILED", extra={"primary": self.primary.name, "fallback": self.fallback.name, "error_type": type(exc).__name__})
            fallback_options = options.model_copy(update={
                "provider": self.fallback.name,
                "voice": self.fallback.default_voice,
                "emotion": "neutral",
                "emotion_intensity": 0.0,
                "style_instruction": "",
            }) if options else None
            output = await self.fallback.synthesize(text, "neutral", fallback_options)
            logger.warning("FALLBACK_TTS_USED", extra={"primary": self.primary.name, "fallback": self.fallback.name})
            return output


class KokoroTTSProvider(TTSProvider):
    """Local CPU ONNX fallback using the unmodified pf_dora speaker."""

    _model_cache: dict[tuple[str, str], object] = {}
    _model_cache_lock = threading.Lock()

    def __init__(
        self,
        model_path: Path,
        voices_path: Path,
        voice: str = KAZUMI_KOKORO_VOICE_ID,
        speaking_rate: float = 0.97,
    ) -> None:
        self.model_path = model_path
        self.voices_path = voices_path
        self.voice = KAZUMI_KOKORO_FALLBACK_VOICE
        self.speaking_rate = speaking_rate
        self.output_dir = DATA_ROOT / "audio"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache = SpeechCache(self.output_dir)
        self._model = None
        self._probe_ok: bool | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "kokoro"

    @property
    def model_id(self) -> str:
        return "hexgrad/Kokoro-82M-v1.0-int8"

    @property
    def default_voice(self) -> str:
        return self.voice

    def describe(self) -> dict[str, object]:
        value = super().describe()
        value.update({
            "voice": self.voice,
            "voice_identity": "KOKORO_PF_DORA_FALLBACK",
            "custom_style": False,
        })
        return value

    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities(supports_native_speed=True)

    @property
    def voices(self) -> list[dict[str, str]]:
        return [
            {
                "id": "pf_dora",
                "name": "Dora — fallback local",
                "language": "pt-BR",
                "gender": "female",
            }
        ]

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        return ("voice", "speaking_rate", "sentence_pause_ms", "paragraph_pause_ms")

    async def _load(self):
        if self._model is None:
            self._model = await asyncio.to_thread(self._load_cached)
        return self._model

    def _load_cached(self):
        key = (str(self.model_path.resolve()), str(self.voices_path.resolve()))
        with self._model_cache_lock:
            model = self._model_cache.get(key)
            if model is None:
                from kokoro_onnx import Kokoro

                model = Kokoro(*key)
                self._model_cache[key] = model
            return model

    @staticmethod
    def _resolve_voice(_model, _voice: str) -> str:
        # The local contingency voice must never interpolate speaker embeddings.
        return KAZUMI_KOKORO_FALLBACK_VOICE

    async def health(self) -> bool:
        if self._probe_ok is not None:
            return self._probe_ok
        if (
            importlib.util.find_spec("kokoro_onnx") is None
            or not self.model_path.is_file()
            or not self.voices_path.is_file()
        ):
            self._probe_ok = False
            return False
        try:
            model = await self._load()
            resolved_voice = self._resolve_voice(model, self.voice)
            samples, rate = await asyncio.to_thread(
                model.create, "Naira online.", voice=resolved_voice, speed=0.97, lang="pt-br"
            )
            self._probe_ok = len(samples) > 100 and rate > 0
        except Exception as exc:
            logger.warning("kokoro_probe_failed", extra={"error_type": type(exc).__name__})
            self._probe_ok = False
        return self._probe_ok

    async def synthesize(
        self,
        text: str,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ) -> Path:
        profile, defaults = load_voice_profile()
        selected = (options or defaults.model_copy(update={
            "provider": self.name,
            "voice": self.voice,
            "speaking_rate": self.speaking_rate,
        })).for_state(state, profile)
        cache_key = selected.cache_key_data(engine=self.name, model=self.model_id, text=text)
        if cached := self.cache.get(cache_key):
            return cached
        model = await self._load()
        resolved_voice = self._resolve_voice(model, selected.voice)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"kazumi-{uuid4().hex}.wav"
        paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
        if not paragraphs:
            raise ValueError("Texto vazio para síntese")
        import soundfile as sf

        pieces: list[np.ndarray] = []
        sample_rate = 24000
        async with self._lock:
            for index, paragraph in enumerate(paragraphs):
                samples, sample_rate = await asyncio.to_thread(
                    model.create,
                    paragraph[:500],
                    voice=resolved_voice,
                    speed=selected.speaking_rate,
                    lang="pt-br",
                    sentence_pause=selected.sentence_pause_ms / 1000,
                    clause_pause=max(0.06, selected.sentence_pause_ms / 2200),
                )
                mono = np.asarray(samples, dtype=np.float32).reshape(-1)
                pieces.append(mono)
                if index < len(paragraphs) - 1:
                    pieces.append(
                        np.zeros(round(sample_rate * selected.paragraph_pause_ms / 1000), dtype=np.float32)
                    )
            joined = self._normalize(np.concatenate(pieces))
            await asyncio.to_thread(sf.write, str(output), joined, sample_rate, subtype="PCM_16")
        if not output.exists() or output.stat().st_size < 100:
            raise RuntimeError("Kokoro não produziu áudio")
        output = await asyncio.to_thread(self.cache.put, cache_key, output)
        logger.info(
            "tts_synthesized",
            extra={
                "provider": self.name,
                "voice": selected.voice,
                "state": state,
                "characters": len(text),
                "bytes": output.stat().st_size,
            },
        )
        return output

    @staticmethod
    def _normalize(samples: np.ndarray) -> np.ndarray:
        peak = float(np.max(np.abs(samples))) if samples.size else 0
        if peak <= 0:
            return samples
        # Conservative peak normalization: no dynamic compression and no clipping.
        return np.clip(samples * min(1.15, 0.92 / peak), -0.96, 0.96)


# Backward-compatible name for integrations created during the MVP.
KokoroOnnxTTS = KokoroTTSProvider


class ChatterboxTTSProvider(TTSProvider):
    """Chatterbox Multilingual V3 isolated from the stable backend environment."""

    def __init__(
        self,
        python_path: Path,
        device: str = "cpu",
        reference_path: Path | None = None,
        model_id: str = "ResembleAI/chatterbox",
        provider_name: str = "chatterbox_multilingual_v3",
        resident: bool = True,
        timeout_seconds: int = 900,
    ) -> None:
        self.python_path = python_path
        self.device = device
        self.reference_path = reference_path
        self.model_id = model_id
        self.provider_name = provider_name
        self.resident = resident
        self.timeout_seconds = timeout_seconds
        self.output_dir = DATA_ROOT / "audio"
        self.request_dir = DATA_ROOT / "tts-requests"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache = SpeechCache(self.output_dir)
        self.request_dir.mkdir(parents=True, exist_ok=True)
        self._health: bool | None = None
        self._server: asyncio.subprocess.Process | None = None
        self._server_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def provider_type(self) -> str:
        return "local"

    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities(
            # Chatterbox exposes native expressiveness controls, but no named
            # emotion conditioning API. The presentation adapter reports PARTIAL.
            supports_emotion=False,
            supports_styles=True,
            supports_reference_style=True,
        )

    @property
    def voices(self) -> list[dict[str, str]]:
        voices = [
            {
                "id": "default",
                "name": "Voz padrão Multilingual V3",
                "language": "pt",
                "gender": "model-default",
            }
        ]
        if self.reference_path and self.reference_path.is_file():
            voices.append({
                "id": "kazumi_reference",
                "name": "Referência autorizada da KAZUMI",
                "language": "pt-BR",
                "gender": "reference",
            })
        return voices

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        return ("voice", "temperature", "exaggeration", "cfg_weight", "seed", "paragraph_pause_ms")

    async def health(self) -> bool:
        if self._health is not None:
            return self._health
        if not self.python_path.is_file():
            self._health = False
            return False
        # Importing PyTorch/Chatterbox on a cold Windows CPU host can exceed 30 s.
        # The probe performs no synthesis or network request, so a wider local timeout
        # avoids caching a false negative for the whole backend lifetime.
        self._health = await self._run(["--probe", "--model-id", self.model_id], timeout=90)
        return self._health

    async def synthesize(
        self,
        text: str,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ) -> Path:
        return await self._synthesize(text, state, options, None)

    async def synthesize_with_reference(
        self,
        text: str,
        reference_path: Path,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ) -> Path:
        """Use an explicitly license-approved candidate without changing KAZUMI_VOICE."""
        if not reference_path.is_file():
            raise ValueError("referência de candidata inexistente")
        return await self._synthesize(text, state, options, reference_path)

    async def _synthesize(
        self,
        text: str,
        state: str,
        options: VoiceSynthesisOptions | None,
        reference_override: Path | None,
    ) -> Path:
        profile, defaults = load_voice_profile()
        selected = (options or defaults).for_state(state, profile)
        cache_key = selected.cache_key_data(engine=self.name, model=self.model_id, text=text)
        if cached := self.cache.get(cache_key):
            return cached
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.request_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"kazumi-{uuid4().hex}.wav"
        request = self.request_dir / f"request-{uuid4().hex}.json"
        reference = reference_override or (self.reference_path if self.reference_path and self.reference_path.is_file() else None)
        payload = {
            "text": text[:4000],
            "output": str(output),
            "device": self.device,
            "language_id": "pt",
            "audio_prompt_path": str(reference) if reference else None,
            "temperature": selected.temperature,
            "exaggeration": selected.exaggeration,
            "cfg_weight": selected.cfg_weight,
            "seed": selected.seed,
            "paragraph_pause_ms": selected.paragraph_pause_ms,
            "model_id": self.model_id,
        }
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            ok = await self._resident_request(payload) if self.resident else await self._run([str(request)], timeout=self.timeout_seconds)
        finally:
            request.unlink(missing_ok=True)
        if not ok or not output.is_file() or output.stat().st_size < 100:
            raise RuntimeError("Chatterbox não produziu áudio; Kokoro permanece disponível")
        output = await asyncio.to_thread(self.cache.put, cache_key, output)
        logger.info(
            "tts_synthesized",
            extra={
                "provider": self.name,
                "state": state,
                "reference": bool(reference),
                "characters": len(text),
                "bytes": output.stat().st_size,
            },
        )
        return output

    async def _resident_request(self, payload: dict) -> bool:
        async with self._server_lock:
            if self._server is None or self._server.returncode is not None:
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(BACKEND_ROOT)
                self._server = await asyncio.create_subprocess_exec(
                    str(self.python_path), "-m", "app.speech.chatterbox_worker", "--server",
                    "--model-id", self.model_id, cwd=str(BACKEND_ROOT), env=environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            assert self._server.stdin is not None and self._server.stdout is not None
            self._server.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await self._server.stdin.drain()
            try:
                line = await asyncio.wait_for(self._server.stdout.readline(), self.timeout_seconds)
            except TimeoutError:
                await self._stop_server()
                return False
            if not line:
                await self._stop_server()
                return False
            try:
                result = json.loads(line.decode())
            except json.JSONDecodeError:
                return False
            return bool(result.get("ok"))

    async def _stop_server(self) -> None:
        process, self._server = self._server, None
        if process and process.returncode is None:
            process.kill()
            await process.wait()

    async def _run(self, arguments: list[str], timeout: float) -> bool:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(BACKEND_ROOT)
        process = await asyncio.create_subprocess_exec(
            str(self.python_path),
            "-m",
            "app.speech.chatterbox_worker",
            *arguments,
            cwd=str(BACKEND_ROOT),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("chatterbox_timeout")
            return False
        if process.returncode != 0:
            logger.warning(
                "chatterbox_worker_failed",
                extra={
                    "return_code": process.returncode,
                    "stderr_tail": stderr.decode(errors="replace")[-500:],
                },
            )
        return process.returncode == 0


class EdgeTTSProvider(TTSProvider):
    """Online Microsoft Edge neural TTS, isolated behind the normal TTS contract."""

    _voice_cache: list[dict[str, str]] | None = None
    _voice_cache_at: float = 0.0
    _voice_cache_lock = asyncio.Lock()

    def __init__(
        self,
        locale: str = "pt-BR",
        gender: str = "Female",
        timeout_seconds: int = 30,
        voice: str = KAZUMI_EDGE_VOICE_ID,
    ) -> None:
        self.locale = locale
        self.gender = gender
        self.timeout_seconds = timeout_seconds
        self.voice = voice
        self.output_dir = DATA_ROOT / "audio"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._health: bool | None = None
        self._voices: list[dict[str, str]] = []
        self.cache_path = DATA_ROOT / "edge-voices.json"

    @property
    def name(self) -> str:
        return "edge_tts"

    @property
    def engine_id(self) -> str:
        return KAZUMI_PRIMARY_ENGINE

    @property
    def model_id(self) -> str:
        return "Microsoft Edge Neural TTS"

    @property
    def default_voice(self) -> str:
        return self.voice

    @property
    def provider_type(self) -> str:
        return "online"

    @property
    def voices(self) -> list[dict[str, str]]:
        return self._voices

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        return ("voice", "edge_rate", "edge_pitch", "edge_volume")

    async def refresh_voices(self, all_voices: bool = False) -> list[dict[str, str]]:
        import time
        async with self._voice_cache_lock:
            if self._voice_cache is None or time.monotonic() - self._voice_cache_at > 3600:
                import edge_tts
                catalog = await asyncio.wait_for(edge_tts.list_voices(), self.timeout_seconds)
                self._voice_cache = [
                    {
                        "id": str(item.get("ShortName", "")),
                        "name": str(item.get("FriendlyName", item.get("ShortName", ""))),
                        "language": str(item.get("Locale", "")),
                        "gender": str(item.get("Gender", "")),
                    }
                    for item in catalog if item.get("ShortName")
                ]
                self._voice_cache_at = time.monotonic()
                await asyncio.to_thread(self.cache_path.write_text, json.dumps(self._voice_cache, ensure_ascii=False, indent=2), "utf-8")
            voices = list(self._voice_cache or [])
        if not all_voices:
            voices = [item for item in voices if item["language"].lower() == self.locale.lower() and item["gender"].lower() == self.gender.lower()]
        self._voices = voices
        return voices

    async def health(self) -> bool:
        try:
            voices = await self.refresh_voices(all_voices=True)
            self._health = any(item["id"] == self.voice for item in voices)
        except Exception as exc:
            logger.info("edge_tts_unavailable", extra={"error_type": type(exc).__name__})
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self._voice_cache = cached
                self._voices = list(cached)
            except Exception:
                self._voices = []
            self._health = False
        return self._health

    async def synthesize(self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None) -> Path:
        profile, defaults = load_voice_profile()
        selected = (options or defaults.model_copy(update={
            "provider": self.name,
            "voice": self.voice,
        })).for_state(state, profile)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        mp3_path = self.output_dir / f"kazumi-edge-{uuid4().hex}.mp3"
        output = self.output_dir / f"kazumi-edge-{uuid4().hex}.wav"
        import edge_tts
        communicate = edge_tts.Communicate(
            text[:6000],
            self.voice,
            rate=selected.edge_rate,
            pitch=selected.edge_pitch,
            volume=selected.edge_volume,
        )
        try:
            await asyncio.wait_for(communicate.save(str(mp3_path)), self.timeout_seconds)
            await asyncio.to_thread(self._decode_mp3, mp3_path, output)
            self._health = True
        except Exception:
            self._health = False
            output.unlink(missing_ok=True)
            raise
        finally:
            mp3_path.unlink(missing_ok=True)
        if not output.is_file() or output.stat().st_size < 100:
            raise RuntimeError("Edge TTS não produziu áudio")
        return output

    @staticmethod
    def _decode_mp3(source: Path, destination: Path) -> None:
        import av
        import soundfile as sf
        container = av.open(str(source))
        frames = [frame.to_ndarray() for frame in container.decode(audio=0)]
        if not frames:
            raise RuntimeError("áudio Edge vazio")
        samples = np.concatenate(frames, axis=1)
        if samples.ndim > 1:
            samples = samples.mean(axis=0)
        samples = samples.astype(np.float32)
        rate = int(container.streams.audio[0].rate or 24000)
        container.close()
        sf.write(str(destination), np.clip(samples, -1, 1), rate, subtype="PCM_16")


class Pyttsx3TTS(TTSProvider):
    """Last-resort Windows SAPI fallback isolated from the API process."""

    def __init__(self, language: str = "pt-BR") -> None:
        self.language = language
        self.output_dir = DATA_ROOT / "audio"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._health: bool | None = None

    @property
    def name(self) -> str:
        return "pyttsx3_windows_sapi"

    async def health(self) -> bool:
        if self._health is None:
            self._health = importlib.util.find_spec("pyttsx3") is not None and await self._run(
                ["--probe"], 12
            )
        return self._health

    async def synthesize(
        self,
        text: str,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ) -> Path:
        profile, defaults = load_voice_profile()
        selected = (options or defaults).for_state(state, profile)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"kazumi-{uuid4().hex}.wav"
        input_path = self.output_dir / f"tts-input-{uuid4().hex}.txt"
        input_path.write_text(text[:6000], encoding="utf-8")
        try:
            ok = await self._run(
                [str(input_path), str(output), str(selected.speaking_rate)], 90
            )
        finally:
            input_path.unlink(missing_ok=True)
        if not ok or not output.is_file() or output.stat().st_size < 100:
            raise RuntimeError("SAPI falhou ou excedeu o timeout")
        return output

    @staticmethod
    def _command(arguments: list[str]) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--sapi-worker", *arguments]
        return [sys.executable, "-m", "app.speech.sapi_worker", *arguments]

    @classmethod
    async def _run(cls, arguments: list[str], timeout: float) -> bool:
        process = await asyncio.create_subprocess_exec(
            *cls._command(arguments),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return await asyncio.wait_for(process.wait(), timeout) == 0
        except TimeoutError:
            process.kill()
            await process.wait()
            return False


class XTTSTTS(TTSProvider):
    def __init__(self, language: str = "pt") -> None:
        self.language = language.split("-")[0].lower()
        self.reference = IDENTITY_ROOT / "kazumi_reference.wav"

    @property
    def name(self) -> str:
        return "xtts_v2"

    async def health(self) -> bool:
        return importlib.util.find_spec("TTS") is not None and self.reference.exists()

    async def synthesize(
        self,
        text: str,
        state: str = "neutral",
        options: VoiceSynthesisOptions | None = None,
    ) -> Path:
        raise RuntimeError("XTTS permanece como adaptador legado; requer ambiente e referência próprios")


async def create_tts_provider(
    provider: str,
    language: str,
    model_path: Path | None = None,
    voices_path: Path | None = None,
    voice: str = KAZUMI_EDGE_VOICE_ID,
    chatterbox_python: Path | None = None,
    chatterbox_device: str = "cpu",
    chatterbox_reference: Path | None = None,
    chatterbox_model_id: str = "ResembleAI/chatterbox",
    chatterbox_ptbr_model_id: str = "ResembleAI/Chatterbox-Multilingual-pt-br",
    chatterbox_resident: bool = True,
    chatterbox_timeout_seconds: int = 900,
    edge_tts_enabled: bool = True,
    edge_tts_locale: str = "pt-BR",
    edge_tts_gender: str = "Female",
    edge_tts_timeout_seconds: int = 30,
    fallback_provider: str = "pyttsx3",
    speaking_rate: float = 0.97,
) -> TTSProvider:
    kokoro = (
        KokoroTTSProvider(model_path, voices_path, KAZUMI_KOKORO_FALLBACK_VOICE, speaking_rate)
        if model_path is not None and voices_path is not None
        else None
    )
    if provider == "kokoro" and kokoro is not None and not (model_path.is_file() and voices_path.is_file()):
        # Falha de packaging nunca pode ficar silenciosa: sem assets válidos o
        # Kokoro NÃO fica READY e a ausência é reportada com os caminhos reais.
        logger.error(
            "tts_kokoro_assets_missing",
            extra={"model": str(model_path), "voices": str(voices_path)},
        )
    chatterbox = (
        ChatterboxTTSProvider(chatterbox_python, chatterbox_device, chatterbox_reference, chatterbox_model_id, "chatterbox_multilingual_v3", chatterbox_resident, chatterbox_timeout_seconds)
        if chatterbox_python is not None
        else None
    )
    edge = EdgeTTSProvider(edge_tts_locale, edge_tts_gender, edge_tts_timeout_seconds, voice) if edge_tts_enabled else None
    if provider == "disabled":
        return DisabledTTS()
    if provider == "edge_tts" and edge:
        # Always retain Edge as primary. A startup outage may use the local
        # fallback, but the next turn retries Ava and recovers without restart.
        if kokoro:
            return FallbackTTSProvider(edge, FallbackTTSProvider(kokoro, Pyttsx3TTS(language)))
        return FallbackTTSProvider(edge, Pyttsx3TTS(language))
    if provider == "chatterbox":
        provider = "chatterbox_multilingual_v3"
    requested = chatterbox
    if provider == "chatterbox_ptbr":
        requested = ChatterboxTTSProvider(chatterbox_python, chatterbox_device, chatterbox_reference, chatterbox_ptbr_model_id, "chatterbox_ptbr", chatterbox_resident, chatterbox_timeout_seconds) if chatterbox_python is not None else None
    if provider in {"chatterbox_multilingual_v3", "chatterbox_ptbr"} and requested and await requested.health():
        if kokoro and await kokoro.health():
            sapi = Pyttsx3TTS(language)
            return FallbackTTSProvider(requested, FallbackTTSProvider(kokoro, sapi))
        return requested
    if provider == "kokoro" and kokoro:
        if await kokoro.health():
            fallback: TTSProvider | None = None
            if fallback_provider == "pyttsx3":
                fallback = Pyttsx3TTS(language)
            elif fallback_provider == "edge_tts" and edge:
                fallback = edge
            return FallbackTTSProvider(kokoro, fallback) if fallback else kokoro
        candidates: list[TTSProvider] = (
            [Pyttsx3TTS(language)] if fallback_provider == "pyttsx3"
            else ([edge] if fallback_provider == "edge_tts" and edge else [])
        )
    elif provider in {"chatterbox_multilingual_v3", "chatterbox_ptbr"} and requested:
        candidates = [requested, *([kokoro] if kokoro else [])]
    elif provider == "xtts":
        candidates = [XTTSTTS(language), *([kokoro] if kokoro else [])]
    elif provider == "pyttsx3":
        candidates = [Pyttsx3TTS(language), *([kokoro] if kokoro else [])]
    else:
        candidates = [
            *([chatterbox] if chatterbox else []),
            *([kokoro] if kokoro else []),
            *([edge] if edge else []),
            Pyttsx3TTS(language),
        ]
    for candidate in candidates:
        if await candidate.health():
            if candidate.name != provider and provider not in {"auto", candidate.name}:
                logger.warning(
                    "tts_fallback_selected",
                    extra={"requested": provider, "selected": candidate.name},
                )
            if isinstance(candidate, KokoroTTSProvider) and fallback_provider == "pyttsx3":
                return FallbackTTSProvider(candidate, Pyttsx3TTS(language))
            return candidate
    if provider != "disabled":
        logger.error(
            "tts_provider_unavailable",
            extra={"requested": provider, "model": str(model_path), "voices": str(voices_path)},
        )
    return DisabledTTS()


def tts_provider_catalog(
    model_path: Path,
    voices_path: Path,
    voice: str,
    chatterbox_python: Path,
    chatterbox_device: str,
    chatterbox_reference: Path,
    chatterbox_model_id: str = "ResembleAI/chatterbox",
    chatterbox_ptbr_model_id: str = "ResembleAI/Chatterbox-Multilingual-pt-br",
    chatterbox_resident: bool = True,
    chatterbox_timeout_seconds: int = 900,
    edge_tts_enabled: bool = True,
    edge_tts_locale: str = "pt-BR",
    edge_tts_gender: str = "Female",
    edge_tts_timeout_seconds: int = 30,
) -> list[TTSProvider]:
    providers: list[TTSProvider] = [
        ChatterboxTTSProvider(chatterbox_python, chatterbox_device, chatterbox_reference, chatterbox_model_id, "chatterbox_multilingual_v3", chatterbox_resident, chatterbox_timeout_seconds),
        ChatterboxTTSProvider(chatterbox_python, chatterbox_device, chatterbox_reference, chatterbox_ptbr_model_id, "chatterbox_ptbr", chatterbox_resident, chatterbox_timeout_seconds),
        KokoroTTSProvider(model_path, voices_path, KAZUMI_KOKORO_FALLBACK_VOICE),
    ]
    if edge_tts_enabled:
        providers.append(EdgeTTSProvider(edge_tts_locale, edge_tts_gender, edge_tts_timeout_seconds, voice))
    return providers
