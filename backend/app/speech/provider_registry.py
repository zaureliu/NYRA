from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path

import httpx

from app.speech.online_providers import ElevenLabsTtsProvider, OnlineTtsProvider, OpenAITtsProvider
from app.speech.profile import VoiceSynthesisOptions
from app.speech.provider_credentials import TtsCredentialBroker
from app.speech.provider_models import TtsProviderError, TtsProviderStatus
from app.speech.tts import TTSProvider, TtsCapabilities
from app.speech.gradium_provider import GradiumTTSProvider
from app.speech.custom_provider import CustomTTSProvider
from app.speech.synthesis_config import UniversalTtsSettings


logger = logging.getLogger("kazumi.voice.registry")


class LocalTtsProvider(TTSProvider):
    """Official logical `local` provider around KAZUMI's existing local engine."""

    def __init__(self, delegate: TTSProvider) -> None:
        self.delegate = delegate

    @property
    def name(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        return "Local"

    @property
    def engine_id(self) -> str:
        return self.delegate.engine_id

    @property
    def active_engine(self) -> str:
        return self.delegate.active_engine

    @property
    def active_voice(self) -> str:
        return self.delegate.active_voice

    @property
    def fallback_active(self) -> bool:
        return self.delegate.fallback_active

    @property
    def fallback_reason(self) -> str | None:
        return self.delegate.fallback_reason

    @property
    def default_voice(self) -> str:
        return self.delegate.default_voice

    @property
    def voices(self) -> list[dict[str, str]]:
        return list(self.delegate.voices)

    @property
    def supported_parameters(self) -> tuple[str, ...]:
        return self.delegate.supported_parameters

    def capabilities(self) -> TtsCapabilities:
        current = self.delegate.capabilities()
        return replace(
            current,
            voice_selection=bool(self.voices),
            pt_br=True,
            offline=True,
        )

    async def health(self) -> bool:
        return await self.delegate.health()

    async def list_voices(self, refresh: bool = False) -> list[dict[str, str]]:
        return await self.delegate.list_voices(refresh=refresh)

    async def list_models(self, refresh: bool = False) -> list[dict[str, object]]:
        return await self.delegate.list_models(refresh=refresh)

    async def cancel(self) -> None:
        await self.delegate.cancel()

    async def synthesize(self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None) -> Path:
        selected = options
        if selected is not None:
            selected = selected.model_copy(update={"voice": self.delegate.default_voice})
        return await self.delegate.synthesize(text, state, selected)

    def describe(self) -> dict[str, object]:
        value = super().describe()
        value.update({"provider_id": "local", "delegate": self.delegate.describe()})
        return value


class TtsProviderRegistry(TTSProvider):
    """Single routing authority between speech intent and audio generation."""

    PROVIDER_IDS = ("local", "openai", "elevenlabs", "gradium", "custom")

    def __init__(
        self,
        local_provider: LocalTtsProvider,
        credentials: TtsCredentialBroker,
        *,
        selected_provider: str = "local",
        fallback_provider: str = "local",
        online_enabled: bool = False,
        speaking_rate: float = 0.97,
    ) -> None:
        self.credentials = credentials
        self.online_enabled = bool(online_enabled)
        self.selected_provider = selected_provider if selected_provider in self.PROVIDER_IDS else "local"
        self.fallback_provider = fallback_provider if fallback_provider == "local" else "local"
        self.speaking_rate = speaking_rate
        self._providers: dict[str, TTSProvider] = {}
        self.register(local_provider)
        self._last_active_provider = "local"
        self._fallback_active = False
        self._fallback_reason: str | None = None
        self._last_latency_ms: float | None = None
        self.universal = UniversalTtsSettings()
        self.last_audio_buffer_delay_ms = None

    def register(self, provider: TTSProvider) -> None:
        provider_id = provider.provider_id
        if provider_id not in self.PROVIDER_IDS:
            raise ValueError(f"unsupported TTS provider id: {provider_id}")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> TTSProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"TTS provider não registrado: {provider_id}") from exc

    def all(self) -> list[TTSProvider]:
        return [self._providers[item] for item in self.PROVIDER_IDS if item in self._providers]

    @property
    def configured_provider(self) -> str:
        return self.selected_provider

    def _selection_validation(self):
        provider = self.get(self.selected_provider)
        if isinstance(provider, OnlineTtsProvider):
            return provider.configuration()
        return None

    @property
    def active_provider(self) -> str:
        if self.selected_provider == "local":
            return "local"
        validation = self._selection_validation()
        if self._fallback_active or (validation is not None and not validation.valid):
            return "local"
        return self.selected_provider

    @property
    def name(self) -> str:
        # Existing consumers report the provider that actually owns the audio.
        return self.active_provider

    @property
    def display_name(self) -> str:
        return "TTS Provider Router"

    @property
    def primary_name(self) -> str:
        return self.selected_provider

    @property
    def fallback_name(self) -> str:
        return self.fallback_provider

    @property
    def engine_id(self) -> str:
        return self.selected_provider

    @property
    def active_engine(self) -> str:
        provider = self.get(self.active_provider)
        return provider.active_engine

    @property
    def active_voice(self) -> str:
        return self.get(self.active_provider).active_voice

    @property
    def default_voice(self) -> str:
        return self.get(self.active_provider).default_voice

    @property
    def fallback_active(self) -> bool:
        if self.selected_provider == "local":
            return bool(self.get("local").fallback_active)
        validation = self._selection_validation()
        return self._fallback_active or bool(validation is not None and not validation.valid)

    @property
    def fallback_reason(self) -> str | None:
        if self.selected_provider == "local":
            return self.get("local").fallback_reason
        if self._fallback_reason:
            return self._fallback_reason
        validation = self._selection_validation()
        return validation.status.value if validation is not None and not validation.valid else None

    @property
    def last_latency_ms(self) -> float | None:
        return self._last_latency_ms

    def capabilities(self) -> TtsCapabilities:
        return self.get(self.active_provider).capabilities()

    def validate_configuration(self) -> tuple[bool, str | None]:
        validation = self._selection_validation()
        if validation is None:
            return True, None
        return validation.valid, validation.reason

    async def health(self) -> bool:
        validation = self._selection_validation()
        if validation is not None and validation.valid:
            return True
        return await self.get("local").health()

    def configure(
        self,
        *,
        selected_provider: str | None = None,
        fallback_provider: str | None = None,
        online_enabled: bool | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        speaking_rate: float | None = None,
    ) -> None:
        if selected_provider is not None:
            if selected_provider not in self.PROVIDER_IDS:
                raise ValueError("Provider TTS desconhecido")
            self.selected_provider = selected_provider
        if fallback_provider is not None:
            if fallback_provider != "local":
                raise ValueError("O fallback V1 deve permanecer local")
            self.fallback_provider = fallback_provider
        if online_enabled is not None:
            self.online_enabled = bool(online_enabled)
        if speaking_rate is not None:
            self.speaking_rate = max(0.7, min(1.3, float(speaking_rate)))
        if provider_id is not None:
            provider = self.get(provider_id)
            if model is not None and hasattr(provider, "model_id"):
                provider.model_id = model
            if voice is not None and hasattr(provider, "voice"):
                provider.voice = voice
            if isinstance(provider, OnlineTtsProvider):
                provider.last_status = provider.configuration().status
        self._fallback_active = False
        self._fallback_reason = None
        self._last_active_provider = self.active_provider

    async def cancel(self) -> None:
        await asyncio.gather(*(provider.cancel() for provider in self.all()), return_exceptions=True)

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self.all()), return_exceptions=True)

    def configure_universal(self, value: UniversalTtsSettings) -> None:
        self.universal = value
        gradium = self.get("gradium")
        gradium.settings = value.gradium
        gradium.voice = value.gradium.voice_id
        gradium.model_id = value.gradium.model
        custom = self.get("custom")
        custom.profile = next((p for p in value.custom_profiles if p.id == value.active_custom_profile), None)
        custom.model_id = custom.profile.model if custom.profile else ""
        for provider in (gradium, custom):
            provider._retry_after = 0
            provider.last_status = provider.configuration().status

    async def _use_local(
        self,
        text: str,
        state: str,
        options: VoiceSynthesisOptions | None,
        reason: str,
    ) -> Path:
        local = self.get("local")
        started = time.perf_counter()
        output = await local.synthesize(text, state, options)
        self._last_active_provider = "local"
        self._fallback_active = self.selected_provider != "local" or local.fallback_active
        self._fallback_reason = reason if self.selected_provider != "local" else local.fallback_reason
        self._last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if self.selected_provider != "local":
            logger.warning(
                "tts_fallback_used",
                extra={
                    "primary_provider": self.selected_provider,
                    "fallback_provider": "local",
                    "reason": reason,
                },
            )
        return output

    async def synthesize(self, text: str, state: str = "neutral", options: VoiceSynthesisOptions | None = None) -> Path:
        selected_options = options.model_copy(update={"speaking_rate": self.speaking_rate}) if options else options
        if self.selected_provider == "local":
            self._fallback_active = False
            self._fallback_reason = None
            return await self._use_local(text, state, selected_options, "")

        provider = self.get(self.selected_provider)
        validation = self._selection_validation()
        if self.selected_provider == "custom" and provider.profile and provider.profile.fallback == "none":
            return await provider.synthesize(text, state, selected_options)
        if validation is not None and not validation.valid:
            return await self._use_local(text, state, selected_options, validation.status.value)
        started = time.perf_counter()
        try:
            timeout = float(getattr(provider, "timeout_seconds", 30.0)) + 1.0
            output = await asyncio.wait_for(provider.synthesize(text, state, selected_options), timeout=timeout)
            self._last_active_provider = self.selected_provider
            self._fallback_active = False
            self._fallback_reason = None
            self._last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            return output
        except asyncio.CancelledError:
            await provider.cancel()
            raise
        except TimeoutError:
            await provider.cancel()
            return await self._use_local(text, state, selected_options, TtsProviderStatus.NETWORK_ERROR.value)
        except TtsProviderError as exc:
            return await self._use_local(text, state, selected_options, exc.status.value)
        except Exception as exc:
            logger.warning(
                "tts_provider_unexpected_failure",
                extra={"provider": self.selected_provider, "error_type": type(exc).__name__},
            )
            return await self._use_local(text, state, selected_options, TtsProviderStatus.PROVIDER_ERROR.value)

    async def stream_audio(self, text: str, state: str = "neutral", options=None):
        from app.speech.stream import AudioPacket
        # Snapshot selection for this sentence. Runtime switching takes effect at
        # the next sentence, never halfway through a provider request.
        selected_id = self.selected_provider
        provider = self.get(selected_id)
        fallback_allowed = not (selected_id == "custom" and provider.profile and provider.profile.fallback == "none")
        selected_options = options.model_copy(update={"speaking_rate": self.speaking_rate}) if options else None
        validation = self._selection_validation()
        if selected_id == "local" or (validation is not None and not validation.valid):
            if not fallback_allowed:
                raise TtsProviderError(validation.status, "Provider Custom indisponível; fallback desabilitado.")
            yield AudioPacket(path=await self._use_local(text, state, selected_options,
                validation.status.value if validation is not None and not validation.valid else ""))
            return
        emitted = False
        started = time.perf_counter()
        self._last_latency_ms = None
        try:
            async with asyncio.timeout(180):
                async for packet in provider.stream_audio(text, state, selected_options):
                    emitted = emitted or bool(packet.pcm or packet.path)
                    self._last_active_provider = selected_id
                    self._fallback_active = False
                    self._fallback_reason = None
                    if self._last_latency_ms is None:
                        self._last_latency_ms = (time.perf_counter() - started) * 1000
                    yield packet
        except asyncio.CancelledError:
            await provider.cancel()
            raise
        except Exception as exc:
            self._fallback_active = True
            self._fallback_reason = exc.status.value if isinstance(exc, TtsProviderError) else "NETWORK_ERROR"
            if emitted or not fallback_allowed:
                # Do not replay a partially heard sentence. Following speech can
                # use local fallback while the conversation itself stays alive.
                raise TtsProviderError(TtsProviderStatus.NETWORK_ERROR, "Fala interrompida pelo provider.") from None
            yield AudioPacket(path=await self._use_local(text, state, selected_options, self._fallback_reason))

    async def provider_metadata(self) -> dict[str, object]:
        providers: list[dict[str, object]] = []
        for provider in self.all():
            if isinstance(provider, OnlineTtsProvider):
                validation = provider.configuration()
                error_states = {
                    TtsProviderStatus.AUTH_ERROR,
                    TtsProviderStatus.QUOTA_ERROR,
                    TtsProviderStatus.RATE_LIMIT_ERROR,
                    TtsProviderStatus.NETWORK_ERROR,
                    TtsProviderStatus.PROVIDER_ERROR,
                    TtsProviderStatus.ERROR,
                    TtsProviderStatus.CONNECTING,
                    TtsProviderStatus.STREAMING,
                }
                status = provider.last_status if provider.last_status in error_states and validation.valid else validation.status
                configured = provider.configured
            else:
                configured = True
                status = TtsProviderStatus.LOCAL_READY if await provider.health() else TtsProviderStatus.DEGRADED
            providers.append({
                "id": provider.provider_id,
                "display_name": provider.display_name,
                "configured": configured,
                "selected": provider.provider_id == self.selected_provider,
                "status": status.value,
                "model": getattr(provider, "model_id", None),
                "voice": provider.default_voice,
                "models": await provider.list_models(refresh=False),
                "voices": await provider.list_voices(refresh=False),
                "capabilities": provider.capabilities().public_dict(),
                "last_latency_ms": getattr(provider, "last_latency_ms", None),
                "latency": provider.latency() if hasattr(provider, "latency") else None,
                "sample_rate": getattr(getattr(provider, "settings", None), "sample_rate", None) or getattr(getattr(provider, "profile", None), "sample_rate", None),
                "alignment": [{"text": t.text, "started_at": t.started_at, "ended_at": t.ended_at} for t in getattr(provider, "alignment", ())],
            })
        return {
            "configured_provider": self.selected_provider,
            "active_provider": self.active_provider,
            "fallback_provider": self.fallback_provider,
            "fallback_active": self.fallback_active,
            "fallback_reason": self.fallback_reason,
            "online_enabled": self.online_enabled,
            "last_latency_ms": self._last_latency_ms,
            "providers": providers,
            "universal": self.universal.model_dump(mode="json"),
            "audio_buffer_delay_ms": self.last_audio_buffer_delay_ms,
        }

    def describe(self) -> dict[str, object]:
        return {
            "engine": self.name,
            "configured_provider": self.selected_provider,
            "active_provider": self.active_provider,
            "fallback_provider": self.fallback_provider,
            "fallback_active": self.fallback_active,
            "fallback_reason": self.fallback_reason,
            "model": getattr(self.get(self.active_provider), "model_id", None),
            "voice": self.active_voice,
            "last_latency_ms": self._last_latency_ms,
            "capabilities": self.capabilities().public_dict(),
        }


def build_tts_provider_registry(
    settings,
    local_engine: TTSProvider,
    credentials: TtsCredentialBroker,
    *,
    openai_transport: httpx.AsyncBaseTransport | None = None,
    elevenlabs_transport: httpx.AsyncBaseTransport | None = None,
) -> TtsProviderRegistry:
    registry = TtsProviderRegistry(
        LocalTtsProvider(local_engine),
        credentials,
        selected_provider=str(getattr(settings, "tts_provider_id", "local")),
        fallback_provider=str(getattr(settings, "tts_provider_fallback", "local")),
        online_enabled=bool(getattr(settings, "tts_online_enabled", False)),
        speaking_rate=float(getattr(settings, "tts_speaking_rate", 0.97)),
    )
    online_flag = lambda: registry.online_enabled
    registry.register(OpenAITtsProvider(
        credentials,
        online_flag,
        model=str(getattr(settings, "tts_openai_model", "gpt-4o-mini-tts")),
        voice=str(getattr(settings, "tts_openai_voice", "coral")),
        timeout_seconds=float(getattr(settings, "tts_online_timeout_seconds", 30)),
        transport=openai_transport,
    ))
    registry.register(ElevenLabsTtsProvider(
        credentials,
        online_flag,
        model=str(getattr(settings, "tts_elevenlabs_model", "eleven_multilingual_v2")),
        voice=str(getattr(settings, "tts_elevenlabs_voice_id", "")),
        timeout_seconds=float(getattr(settings, "tts_online_timeout_seconds", 30)),
        transport=elevenlabs_transport,
    ))
    registry.register(GradiumTTSProvider(credentials, online_flag))
    registry.register(CustomTTSProvider(credentials, online_flag))
    registry.configure_universal(getattr(settings, "tts_universal", UniversalTtsSettings()))
    return registry
