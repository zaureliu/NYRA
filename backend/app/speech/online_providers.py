from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.core.paths import DATA_ROOT
from app.speech.audio_format import decode_audio_response_to_wav, write_wav_response
from app.speech.profile import VoiceSynthesisOptions
from app.speech.provider_credentials import TtsCredentialBroker
from app.speech.provider_models import ProviderValidation, TtsProviderError, TtsProviderStatus
from app.speech.style import VoiceStylePlan
from app.speech.tts import TTSProvider, TtsCapabilities


logger = logging.getLogger("kazumi.voice.providers")


class OnlineTtsProvider(TTSProvider):
    async def stream_audio(self, text: str, state: str = "neutral", options=None):
        from app.speech.stream import AudioPacket
        validation = self.configuration()
        self.last_status = validation.status
        if not validation.valid:
            raise TtsProviderError(validation.status, "Provider de voz não configurado.")
        secret = self.credentials.get_for_authorized_provider(self.provider_id)
        if not secret:
            raise TtsProviderError(TtsProviderStatus.NOT_CONFIGURED, "Provider de voz sem credencial.")
        plan = VoiceStylePlan.from_options(options)
        if self.provider_id == "openai":
            url = f"{self.base_url}/audio/speech"
            headers = {"Authorization": f"Bearer {secret}"}
            payload = {"model": self.model_id, "voice": self.voice, "input": text[:12000], "response_format": "pcm",
                       "speed": max(.25, min(4., float(options.speaking_rate if options else 1.)))}
            if self.capabilities().style_instructions:
                payload["instructions"] = self._style_instruction(plan)
            params = None
        else:
            url = f"{self.base_url}/v1/text-to-speech/{quote(self.voice, safe='')}/stream"
            headers = {"xi-api-key": secret}
            voice_settings = {"speed": max(.7, min(1.2, float(options.speaking_rate if options else 1.)))}
            if self.capabilities().supports_styles:
                voice_settings["style"] = max(0., min(1., plan.intensity))
            payload = {"model_id": self.model_id, "text": text[:10000], "voice_settings": voice_settings}
            params = {"output_format": "pcm_24000"}
        task = self._begin_request()
        started = time.perf_counter()
        self.last_latency_ms = None
        try:
            async with self._client() as client:
                async with client.stream("POST", url, headers=headers, json=payload, params=params) as response:
                    if response.status_code >= 400:
                        raise self._remote_failure(response)
                    self.last_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    pending = b""
                    total = 0
                    async for data in response.aiter_bytes(chunk_size=19200):
                        total += len(data)
                        if total > 24000 * 2 * 180:
                            raise TtsProviderError(TtsProviderStatus.PROVIDER_ERROR, "Resposta de áudio excedeu o limite.")
                        pending += data
                        aligned = len(pending) // 2 * 2
                        if aligned:
                            if self.last_latency_ms is None:
                                self.last_latency_ms = (time.perf_counter() - started) * 1000
                            yield AudioPacket(pcm=pending[:aligned])
                            pending = pending[aligned:]
                    if pending or total == 0:
                        raise TtsProviderError(TtsProviderStatus.PROVIDER_ERROR, "Resposta de áudio inválida.")
                    self.last_status = TtsProviderStatus.READY
        except (httpx.TimeoutException, httpx.NetworkError):
            self.last_status = TtsProviderStatus.NETWORK_ERROR
            raise TtsProviderError(self.last_status, "Falha de rede no provider de voz.") from None
        except TtsProviderError as exc:
            self.last_status = exc.status
            raise
        finally:
            self._finish_request(task)

    def __init__(
        self,
        credentials: TtsCredentialBroker,
        online_enabled: Callable[[], bool],
        *,
        model: str,
        voice: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = credentials
        self.online_enabled = online_enabled
        self.model_id = model
        self.voice = voice
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.output_dir = DATA_ROOT / "audio"
        self.last_status = TtsProviderStatus.DISABLED
        self.last_latency_ms: float | None = None
        self.last_request_id: str | None = None
        self._active_tasks: set[asyncio.Task] = set()

    @property
    def provider_type(self) -> str:
        return "online"

    @property
    def default_voice(self) -> str:
        return self.voice

    @property
    def configured(self) -> bool:
        return self.credentials.has_credential(self.provider_id)

    def _common_validation(self) -> ProviderValidation | None:
        if not self.online_enabled():
            return ProviderValidation(False, TtsProviderStatus.DISABLED, "online_disabled")
        if not self.configured:
            return ProviderValidation(False, TtsProviderStatus.NOT_CONFIGURED, "credential_missing")
        return None

    async def health(self) -> bool:
        validation = self.configuration()
        self.last_status = validation.status
        return validation.valid

    def validate_configuration(self) -> tuple[bool, str | None]:
        validation = self.configuration()
        return validation.valid, validation.reason

    def configuration(self) -> ProviderValidation:
        raise NotImplementedError

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            transport=self.transport,
            follow_redirects=False,
        )

    async def cancel(self) -> None:
        current = asyncio.current_task()
        for task in tuple(self._active_tasks):
            if task is not current and not task.done():
                task.cancel()

    def _begin_request(self) -> asyncio.Task | None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        return task

    def _finish_request(self, task: asyncio.Task | None) -> None:
        if task is not None:
            self._active_tasks.discard(task)

    @staticmethod
    def _remote_failure(response: httpx.Response) -> TtsProviderError:
        status_code = response.status_code
        if status_code in {401, 403}:
            return TtsProviderError(TtsProviderStatus.AUTH_ERROR, "Credencial rejeitada pelo provider de voz.")
        if status_code == 402:
            return TtsProviderError(TtsProviderStatus.QUOTA_ERROR, "Créditos do provider de voz indisponíveis.")
        if status_code == 429:
            marker = ""
            try:
                document = response.json()
                marker = str(document).casefold()
            except Exception:
                pass
            if any(value in marker for value in ("quota", "billing", "credit", "insufficient")):
                return TtsProviderError(TtsProviderStatus.QUOTA_ERROR, "Quota do provider de voz esgotada.")
            return TtsProviderError(TtsProviderStatus.RATE_LIMIT_ERROR, "Limite temporário do provider de voz atingido.")
        if status_code in {408, 425} or status_code >= 500:
            return TtsProviderError(TtsProviderStatus.NETWORK_ERROR, "Provider de voz temporariamente indisponível.")
        return TtsProviderError(TtsProviderStatus.PROVIDER_ERROR, "Provider de voz recusou a solicitação.")

    async def _post_audio(self, url: str, *, headers: dict[str, str], json: dict, params: dict | None = None) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.post(url, headers=headers, json=json, params=params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TtsProviderError(TtsProviderStatus.NETWORK_ERROR, "Falha de rede no provider de voz.") from exc
        if response.status_code >= 400:
            raise self._remote_failure(response)
        return response

    def describe(self) -> dict[str, object]:
        value = super().describe()
        value.update({
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "status": self.last_status.value,
            "configured": self.configured,
            "last_latency_ms": self.last_latency_ms,
            "last_request_id": self.last_request_id,
        })
        return value


class OpenAITtsProvider(OnlineTtsProvider):
    """OpenAI Speech API adapter.

    Contract verified against the official Speech endpoint and TTS guide:
    https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create
    https://developers.openai.com/api/docs/guides/text-to-speech
    """

    MODELS = (
        {"id": "gpt-4o-mini-tts", "name": "GPT-4o Mini TTS", "style_instructions": True},
        {"id": "tts-1", "name": "TTS-1", "style_instructions": False},
        {"id": "tts-1-hd", "name": "TTS-1 HD", "style_instructions": False},
    )
    VOICE_IDS = (
        "alloy", "ash", "ballad", "coral", "echo", "fable", "nova",
        "onyx", "sage", "shimmer", "verse", "marin", "cedar",
    )

    def __init__(self, *args, base_url: str = "https://api.openai.com/v1", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "openai"

    @property
    def display_name(self) -> str:
        return "OpenAI"

    @property
    def voices(self) -> list[dict[str, str]]:
        return [{"id": voice, "name": voice.title(), "language": "multilingual"} for voice in self.VOICE_IDS]

    async def list_models(self, refresh: bool = False) -> list[dict[str, object]]:
        del refresh
        return [dict(model) for model in self.MODELS]

    def capabilities(self) -> TtsCapabilities:
        native_style = self.model_id == "gpt-4o-mini-tts"
        return TtsCapabilities(
            supports_emotion=native_style,
            supports_styles=native_style,
            supports_streaming=True,
            supports_native_speed=True,
            style_instructions=native_style,
            voice_selection=True,
            voice_cloning=False,
            pt_br=True,
            offline=False,
        )

    def configuration(self) -> ProviderValidation:
        if common := self._common_validation():
            return common
        if self.model_id not in {str(item["id"]) for item in self.MODELS}:
            return ProviderValidation(False, TtsProviderStatus.DEGRADED, "unsupported_model")
        allowed = set(self.VOICE_IDS)
        if self.model_id in {"tts-1", "tts-1-hd"}:
            allowed -= {"ballad", "verse", "marin", "cedar"}
        if self.voice not in allowed:
            return ProviderValidation(False, TtsProviderStatus.DEGRADED, "unsupported_voice")
        return ProviderValidation(True, TtsProviderStatus.READY)

    @staticmethod
    def _style_instruction(plan: VoiceStylePlan) -> str:
        if plan.instruction:
            return plan.instruction
        if plan.emotion == "neutral" and plan.intensity < 0.05 and plan.pace == "normal":
            return ""
        return (
            f"Speak with a {plan.emotion} emotion at {plan.intensity:.2f} intensity, "
            f"a {plan.pace} pace, and {plan.energy} energy."
        )

    async def synthesize(self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None) -> Path:
        del state
        validation = self.configuration()
        self.last_status = validation.status
        if not validation.valid:
            raise TtsProviderError(validation.status, "OpenAI TTS não está configurado para uso.")
        secret = self.credentials.get_for_authorized_provider(self.provider_id)
        if not secret:
            self.last_status = TtsProviderStatus.NOT_CONFIGURED
            raise TtsProviderError(self.last_status, "OpenAI TTS sem credencial.")
        speech_text = text.strip()
        if not speech_text:
            raise TtsProviderError(TtsProviderStatus.PROVIDER_ERROR, "Texto de fala vazio.")
        plan = VoiceStylePlan.from_options(options)
        payload: dict[str, object] = {
            "model": self.model_id,
            "input": speech_text[:12000],
            "voice": self.voice,
            "response_format": "wav",
            "speed": max(0.25, min(4.0, float(options.speaking_rate if options else 1.0))),
        }
        instruction = self._style_instruction(plan)
        if instruction and self.capabilities().style_instructions:
            payload["instructions"] = instruction
        started = time.perf_counter()
        task = self._begin_request()
        try:
            response = await self._post_audio(
                f"{self.base_url}/audio/speech",
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                json=payload,
            )
            destination = self.output_dir / f"kazumi-openai-{uuid4().hex}.wav"
            output = await asyncio.to_thread(write_wav_response, response.content, destination)
            self.last_request_id = response.headers.get("x-request-id")
            self.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            self.last_status = TtsProviderStatus.READY
            return output
        except asyncio.CancelledError:
            raise
        except TtsProviderError as exc:
            self.last_status = exc.status
            logger.warning("tts_provider_failed", extra={"provider": self.provider_id, "status": exc.status.value})
            raise
        finally:
            self._finish_request(task)

class ElevenLabsTtsProvider(OnlineTtsProvider):
    """ElevenLabs Text-to-Speech REST adapter using the current official API."""

    STATIC_MODELS = (
        {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2", "can_use_style": True},
        {"id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5", "can_use_style": True},
    )
    _VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{3,128}$")

    def __init__(self, *args, base_url: str = "https://api.elevenlabs.io", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self._voices: list[dict[str, str]] = []
        self._models: list[dict[str, object]] = [dict(item) for item in self.STATIC_MODELS]

    @property
    def name(self) -> str:
        return "elevenlabs"

    @property
    def display_name(self) -> str:
        return "ElevenLabs"

    @property
    def voices(self) -> list[dict[str, str]]:
        if self._voices:
            return list(self._voices)
        if self.voice:
            return [{"id": self.voice, "name": "Voice ID configurado", "language": "provider-managed"}]
        return []

    async def list_models(self, refresh: bool = False) -> list[dict[str, object]]:
        if refresh:
            await self.refresh_catalog()
        return list(self._models)

    async def list_voices(self, refresh: bool = False) -> list[dict[str, str]]:
        if refresh:
            await self.refresh_catalog()
        return self.voices

    def capabilities(self) -> TtsCapabilities:
        selected = next((item for item in self._models if item.get("id") == self.model_id), {})
        style = bool(selected.get("can_use_style", True))
        return TtsCapabilities(
            # ElevenLabs exposes style/stability controls, not a named
            # provider-native emotion contract. Report this honestly as partial.
            supports_emotion=False,
            supports_styles=style,
            supports_streaming=True,
            supports_native_speed=True,
            style_instructions=False,
            voice_selection=True,
            voice_cloning=False,
            pt_br=True,
            offline=False,
        )

    def configuration(self) -> ProviderValidation:
        if common := self._common_validation():
            return common
        if not self.model_id or self.model_id not in {str(item.get("id")) for item in self._models}:
            return ProviderValidation(False, TtsProviderStatus.DEGRADED, "unsupported_model")
        if not self.voice or not self._VOICE_ID.fullmatch(self.voice):
            return ProviderValidation(False, TtsProviderStatus.DEGRADED, "voice_id_required")
        return ProviderValidation(True, TtsProviderStatus.READY)

    async def refresh_catalog(self) -> None:
        common = self._common_validation()
        if common:
            self.last_status = common.status
            raise TtsProviderError(common.status, "ElevenLabs indisponível para catálogo.")
        secret = self.credentials.get_for_authorized_provider(self.provider_id)
        if not secret:
            raise TtsProviderError(TtsProviderStatus.NOT_CONFIGURED, "ElevenLabs sem credencial.")
        headers = {"xi-api-key": secret, "Accept": "application/json"}
        try:
            async with self._client() as client:
                models_response, voices_response = await asyncio.gather(
                    client.get(f"{self.base_url}/v1/models", headers=headers),
                    client.get(
                        f"{self.base_url}/v2/voices",
                        headers=headers,
                        params={"page_size": 100, "include_total_count": "false"},
                    ),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            self.last_status = TtsProviderStatus.NETWORK_ERROR
            raise TtsProviderError(self.last_status, "Falha de rede ao atualizar catálogo ElevenLabs.") from exc
        for response in (models_response, voices_response):
            if response.status_code >= 400:
                failure = self._remote_failure(response)
                self.last_status = failure.status
                raise failure
        models_document = models_response.json()
        voices_document = voices_response.json()
        models = [
            {
                "id": str(item.get("model_id")),
                "name": str(item.get("name") or item.get("model_id")),
                "can_use_style": bool(item.get("can_use_style")),
            }
            for item in models_document
            if isinstance(item, dict) and item.get("model_id") and item.get("can_do_text_to_speech")
        ] if isinstance(models_document, list) else []
        raw_voices = voices_document.get("voices", []) if isinstance(voices_document, dict) else []
        voices = [
            {
                "id": str(item.get("voice_id")),
                "name": str(item.get("name") or item.get("voice_id")),
                "language": str((item.get("labels") or {}).get("language") or "provider-managed"),
            }
            for item in raw_voices
            if isinstance(item, dict) and item.get("voice_id")
        ]
        if models:
            self._models = models
        self._voices = voices
        self.last_status = TtsProviderStatus.READY

    async def synthesize(self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None) -> Path:
        del state
        validation = self.configuration()
        self.last_status = validation.status
        if not validation.valid:
            raise TtsProviderError(validation.status, "ElevenLabs TTS não está configurado para uso.")
        secret = self.credentials.get_for_authorized_provider(self.provider_id)
        if not secret:
            self.last_status = TtsProviderStatus.NOT_CONFIGURED
            raise TtsProviderError(self.last_status, "ElevenLabs TTS sem credencial.")
        speech_text = text.strip()
        if not speech_text:
            raise TtsProviderError(TtsProviderStatus.PROVIDER_ERROR, "Texto de fala vazio.")
        plan = VoiceStylePlan.from_options(options)
        speed = max(0.7, min(1.2, float(options.speaking_rate if options else 1.0)))
        voice_settings: dict[str, object] = {"speed": speed}
        if self.capabilities().supports_styles and plan.intensity > 0:
            voice_settings.update({
                "stability": round(max(0.2, min(0.8, 0.58 - plan.intensity * 0.28)), 3),
                "style": round(max(0.0, min(1.0, plan.intensity)), 3),
            })
        payload: dict[str, object] = {
            "text": speech_text[:10000],
            "model_id": self.model_id,
            "voice_settings": voice_settings,
        }
        started = time.perf_counter()
        task = self._begin_request()
        try:
            response = await self._post_audio(
                f"{self.base_url}/v1/text-to-speech/{quote(self.voice, safe='')}",
                headers={"xi-api-key": secret, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json=payload,
                params={"output_format": "mp3_44100_128"},
            )
            destination = self.output_dir / f"kazumi-elevenlabs-{uuid4().hex}.wav"
            output = await asyncio.to_thread(decode_audio_response_to_wav, response.content, destination)
            self.last_request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
            self.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            self.last_status = TtsProviderStatus.READY
            return output
        except asyncio.CancelledError:
            raise
        except TtsProviderError as exc:
            self.last_status = exc.status
            logger.warning("tts_provider_failed", extra={"provider": self.provider_id, "status": exc.status.value})
            raise
        finally:
            self._finish_request(task)
