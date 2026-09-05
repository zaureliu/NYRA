"""Secret-free, declarative TTS configuration. No executable templates."""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLACEHOLDERS = {"text", "voice_id", "model", "language", "sample_rate", "output_format", "emotion", "style", "speed"}
SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|password|secret|token|authorization|cookie|credential)", re.I)
SECRET_VALUE = re.compile(r"(?:\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,})", re.I)


def validate_template(value: Any, depth: int = 0) -> None:
    if depth > 12 or len(json.dumps(value)) > 24000:
        raise ValueError("Template excede os limites.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE_KEY.search(key) or "{{" in key:
                raise ValueError("Autenticação pertence somente ao Credential Broker/header.")
            validate_template(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_template(item, depth + 1)
    elif isinstance(value, str):
        if SECRET_VALUE.search(value):
            raise ValueError("Não inclua credenciais no template.")
        remainder = re.sub(r"\{\{(\w+)\}\}", lambda m: "" if m[1] in PLACEHOLDERS else m[0], value)
        if "{{" in remainder or "}}" in remainder:
            raise ValueError("Placeholder desconhecido; expressões executáveis não são permitidas.")


def substitute(value: Any, context: dict[str, Any]) -> Any:
    """Walk parsed JSON, preserving typed whole-value placeholders and escaping strings via JSON."""
    if isinstance(value, dict):
        return {key: substitute(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, context) for item in value]
    if isinstance(value, str):
        match = re.fullmatch(r"\{\{(\w+)\}\}", value)
        if match:
            return context[match[1]]
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(context[m[1]]), value)
    return value


def validate_endpoint(endpoint: str, transport: str, allow_loopback: bool = False):
    parsed = urlsplit(endpoint)
    secure, local = ("https", "http") if transport == "rest" else ("wss", "ws")
    if (parsed.scheme not in (secure, local) or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in endpoint)):
        raise ValueError("URL TTS inválida: use HTTPS/WSS, sem credenciais, query ou fragmento.")
    host = parsed.hostname
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == local and not (allow_loopback and loopback):
        raise ValueError("HTTP/WS somente em loopback explicitamente permitido.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (loopback and not allow_loopback) or (address and not address.is_global and not (allow_loopback and loopback)):
        raise ValueError("Endereço privado não autorizado para TTS.")
    if allow_loopback and not loopback:
        raise ValueError("A opção local exige hostname loopback explícito.")
    _ = parsed.port  # Reject invalid port syntax now, not at connection time.
    return parsed


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class GradiumOptions(StrictSettings):
    temp: float = Field(0.7, ge=0, le=1.4)
    cfg_coef: float = Field(2, ge=1, le=4)
    padding_bonus: float = Field(0, ge=-4, le=4)
    rewrite_rules: Literal["en", "fr", "fr-be", "fr-ch", "de", "es", "pt", "none"] | None = None


class GradiumSettings(StrictSettings):
    endpoint: Literal["wss://api.gradium.ai/api/speech/tts", "wss://eu.api.gradium.ai/api/speech/tts", "wss://us.api.gradium.ai/api/speech/tts"] = "wss://api.gradium.ai/api/speech/tts"
    voice_id: str = Field("", max_length=128, pattern=r"^[\w-]*$")
    model: str = Field("default", min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    sample_rate: Literal[16000, 24000, 48000] = 48000
    pronunciation_id: str = Field("", max_length=128, pattern=r"^[\w-]*$")
    json_config: GradiumOptions = Field(default_factory=GradiumOptions)


class CustomProfile(StrictSettings):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(max_length=2048)
    transport: Literal["rest", "websocket"] = "rest"
    allow_loopback: bool = False
    auth_type: Literal["bearer", "api_key_header", "custom_header", "none"] = "bearer"
    header_name: str = Field("x-api-key", pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    voice_id: str = Field("", max_length=128)
    model: str = Field("", max_length=128)
    language: str = Field("pt-BR", max_length=32)
    sample_rate: Literal[16000, 24000, 48000] = 48000
    output_format: Literal["pcm_s16le", "wav", "mp3", "ogg"] = "pcm_s16le"
    streaming: bool = True
    fallback: Literal["local", "none"] = "local"
    request_template: dict[str, Any] = Field(default_factory=lambda: {"text": "{{text}}", "voice": "{{voice_id}}"})
    setup_template: dict[str, Any] | None = None
    text_template: dict[str, Any] = Field(default_factory=lambda: {"type": "text", "text": "{{text}}"})
    end_template: dict[str, Any] | None = None
    cancel_template: dict[str, Any] | None = None
    response_mode: Literal["RAW_AUDIO_BYTES", "JSON_BASE64_AUDIO", "WEBSOCKET_BINARY_FRAMES", "WEBSOCKET_JSON_BASE64"] = "RAW_AUDIO_BYTES"
    audio_field: str = Field("audio", pattern=r"^[\w.-]{1,128}$")
    event_type_field: str = Field("type", pattern=r"^[\w.-]{1,128}$")
    audio_event_value: str = Field("audio", max_length=64)
    ready_event_value: str = Field("", max_length=64)
    end_event_value: str = Field("end_of_stream", max_length=64)

    @property
    def credential_provider(self) -> str:
        return f"custom:{self.id}"

    @model_validator(mode="after")
    def safe_contract(self):
        validate_endpoint(self.endpoint, self.transport, self.allow_loopback)
        if self.header_name.lower() in {"host", "content-length", "transfer-encoding", "connection", "upgrade", "proxy-authorization", "cookie", "set-cookie"} or self.header_name.lower().startswith("sec-"):
            raise ValueError("Header reservado não pode conter autenticação customizada.")
        ws_mode = self.response_mode.startswith("WEBSOCKET_")
        if ws_mode != (self.transport == "websocket"):
            raise ValueError("Response mode incompatível com transport.")
        if self.streaming and (self.output_format != "pcm_s16le" or self.response_mode == "JSON_BASE64_AUDIO"):
            raise ValueError("Streaming incremental requer PCM e resposta raw/WS; demais formatos são buffered.")
        for key in ("request_template", "setup_template", "text_template", "end_template", "cancel_template"):
            validate_template(getattr(self, key))
        for key in ("name", "voice_id", "model", "language"):
            if SECRET_VALUE.search(getattr(self, key)):
                raise ValueError("Não inclua segredos na configuração.")
        return self


class UniversalTtsSettings(StrictSettings):
    gradium: GradiumSettings = Field(default_factory=GradiumSettings)
    custom_profiles: list[CustomProfile] = Field(default_factory=list, max_length=32)
    active_custom_profile: str | None = None

    @model_validator(mode="after")
    def unique_profiles(self):
        ids = [profile.id for profile in self.custom_profiles]
        if len(ids) != len(set(ids)) or (self.active_custom_profile is not None and self.active_custom_profile not in ids):
            raise ValueError("Perfil duplicado ou seleção inexistente.")
        return self
