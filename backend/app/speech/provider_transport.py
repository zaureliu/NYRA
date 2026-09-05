"""Bounded transport/audio utilities shared by new TTS adapters, not another queue."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import socket
import time
import wave
from collections import deque
from urllib.parse import urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.speech.online_providers import OnlineTtsProvider
from app.speech.provider_models import TtsProviderError, TtsProviderStatus
from app.speech.stream import AudioPacket
from app.speech.synthesis_config import validate_endpoint

MAX_AUDIO_BYTES = 48000 * 2 * 180
MAX_MESSAGE_BYTES = 1024 * 1024
# Never let third-party debug logging dump handshake authorization headers.
WIRE_LOGGER = logging.Logger("nyra.tts.private_wire", level=logging.CRITICAL + 1)


class NoRedirectConnect(connect):
    def process_redirect(self, exc):
        return exc


async def receive(ws, seconds=30):
    # asyncio.timeout doesn't spawn a second receiver task; cancellation stays
    # owned by SpeechQueue even if data arrives simultaneously with barge-in.
    async with asyncio.timeout(seconds):
        value = await ws.recv()
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError
    return value


async def resolve_endpoint(endpoint: str, transport: str, allow_loopback: bool = False):
    parsed = validate_endpoint(endpoint, transport, allow_loopback)
    port = parsed.port or (443 if parsed.scheme in {"wss", "https"} else 80)
    addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Endpoint indisponível.")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global and not (allow_loopback and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and ip.is_loopback):
            raise ValueError("Resolução privada não autorizada.")
    return parsed, addresses[0]


async def open_socket(endpoint: str, headers: dict, allow_loopback: bool = False):
    parsed, (family, kind, proto, _, address) = await resolve_endpoint(endpoint, "websocket", allow_loopback)
    # Pin the validated IP to the connection: no second DNS lookup/rebinding.
    sock = socket.socket(family, kind, proto)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, address), 10)
        return await NoRedirectConnect(endpoint, additional_headers=headers, sock=sock,
            proxy=None, open_timeout=10, close_timeout=2, ping_interval=20, ping_timeout=10,
            max_size=MAX_MESSAGE_BYTES, max_queue=8, compression=None, logger=WIRE_LOGGER)
    except BaseException:
        sock.close()
        raise


async def pinned_http(endpoint: str, allow_loopback: bool = False):
    parsed, (_, _, _, _, address) = await resolve_endpoint(endpoint, "rest", allow_loopback)
    host = parsed.hostname
    address_host = f"[{address[0]}]" if ":" in address[0] else address[0]
    url = urlunsplit((parsed.scheme, f"{address_host}:{address[1]}", parsed.path or "/", "", ""))
    return url, {"Host": parsed.netloc}, {"sni_hostname": host}


def safe_failure(exc: Exception) -> TtsProviderError:
    if isinstance(exc, TtsProviderError):
        return exc
    code = exc.response.status_code if isinstance(exc, InvalidStatus) else None
    if code in (401, 403):
        return TtsProviderError(TtsProviderStatus.AUTH_ERROR, "Credencial rejeitada pelo provider de voz.")
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return TtsProviderError(TtsProviderStatus.ERROR, "Resposta ou configuração de áudio inválida.")
    return TtsProviderError(TtsProviderStatus.NETWORK_ERROR, "Conexão TTS indisponível ou interrompida.")


class StreamingTtsProvider(OnlineTtsProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._metrics = deque(maxlen=100)
        self.last_metrics: dict = {}
        self._retry_after = 0.0
        self._failures = 0

    def begin_timing(self):
        self.last_metrics = {}
        self.last_latency_ms = None
        return time.perf_counter()

    def mark(self, name, started):
        self.last_metrics[name] = round((time.perf_counter() - started) * 1000, 2)

    def first_audio(self, started):
        if self.last_latency_ms is None:
            self.mark("request_to_first_audio_ms", started)
            self.last_latency_ms = self.last_metrics["request_to_first_audio_ms"]
            self.last_metrics["text_to_first_audio_ms"] = max(0, self.last_latency_ms - self.last_metrics.get("text_sent_ms", 0))
            self._metrics.append(self.last_latency_ms)
        self.last_status = TtsProviderStatus.STREAMING

    def latency(self):
        values = sorted(self._metrics)
        return {"samples": len(values), "last": self.last_metrics,
            "avg_ms": round(sum(values) / len(values), 2) if values else None,
            "p50_ms": values[int((len(values) - 1) * .5)] if len(values) >= 5 else None,
            "p95_ms": values[int((len(values) - 1) * .95)] if len(values) >= 20 else None}

    def check_ready(self):
        validation = self.configuration()
        if not validation.valid:
            self.last_status = validation.status
            raise TtsProviderError(validation.status, "Provider não configurado ou desabilitado.")
        if time.monotonic() < self._retry_after:
            raise TtsProviderError(self.last_status, "Provider em recuperação; fallback disponível.")

    def failed(self, exc):
        error = safe_failure(exc)
        self.last_status = error.status
        self._failures = min(6, self._failures + 1)
        self._retry_after = time.monotonic() + min(30, 2 ** self._failures)
        return error

    def succeeded(self):
        self._failures = 0
        self._retry_after = 0
        self.last_status = TtsProviderStatus.READY

    async def synthesize(self, text, state="neutral", options=None):
        # Compatibility path for existing nonstreaming consumers only. The
        # conversation and test-voice paths consume stream_audio directly.
        data = bytearray()
        rate = 48000
        async for packet in self.stream_audio(text, state, options):
            if packet.path:
                return packet.path
            rate = packet.sample_rate
            data.extend(packet.pcm)
            if len(data) > MAX_AUDIO_BYTES:
                raise ValueError("Áudio excedeu o limite.")
        if not data:
            raise TtsProviderError(TtsProviderStatus.ERROR, "Provider não retornou áudio.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"nyra-{self.provider_id}-{uuid4().hex}.wav"
        with wave.open(str(output), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(data)
        return output.resolve()


def pcm_packets(data: bytes, rate: int):
    if len(data) % 2:
        raise ValueError("PCM desalinhado.")
    # 80 ms packets at the configured native rate; bounds match the player.
    size = rate * 2 * 80 // 1000
    for offset in range(0, len(data), size):
        yield AudioPacket(pcm=data[offset:offset + size], sample_rate=rate)


def decode_buffered(data: bytes, format: str, rate: int):
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError("Áudio vazio ou excessivo.")
    if format == "pcm_s16le":
        return data
    # Existing PyAV dependency, no downloaded decoder/executable. Resample only
    # buffered compressed/container formats, with strict decoded duration bound.
    import av
    output = bytearray()
    with av.open(io.BytesIO(data), format={"wav": "wav", "mp3": "mp3", "ogg": "ogg"}[format]) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(audio=0):
            for normalized in resampler.resample(frame):
                output.extend(normalized.to_ndarray().tobytes())
                if len(output) > rate * 2 * 180:
                    raise ValueError("Duracao de áudio excessiva.")
        for normalized in resampler.resample(None):
            output.extend(normalized.to_ndarray().tobytes())
    if not output or len(output) > rate * 2 * 180:
        raise ValueError("Áudio decodificado inválido.")
    return bytes(output)
