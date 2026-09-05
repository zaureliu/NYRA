from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from contextlib import suppress
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .benchmark import PHRASES, score
from .deepgram import DeepgramSTTProvider
from .models import AudioFormat, STTFailure, STTSettings, STTState


router = APIRouter(prefix="/api/stt", tags=["speech recognition"])


def require_loopback(connection) -> None:
    try:
        allowed = bool(connection.client and ipaddress.ip_address(connection.client.host).is_loopback)
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(403, "STT requires a loopback connection")


class StreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["listening", "direct", "diagnostic"] = "direct"
    client_id: str = Field("", max_length=100)
    audio_format: AudioFormat = Field(default_factory=AudioFormat)
    mic_started_at: float | None = None
    benchmark: bool = False
    reference: str = Field("", max_length=2000)


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    api_key: SecretStr


@router.get("/settings")
async def settings(request: Request):
    return await request.app.state.services.stt.status()


@router.put("/settings")
async def update_settings(request: Request, config: STTSettings):
    require_loopback(request)
    return await request.app.state.services.stt.update(config)


@router.put("/credential")
async def credential_save(request: Request):
    require_loopback(request)
    # Parse ourselves so validation errors cannot echo the request's secret.
    try:
        body = await request.body()
        if len(body) > 8192:
            raise ValueError()
        payload = CredentialInput.model_validate_json(body)
        secret = payload.api_key.get_secret_value().strip()
        if not secret or len(secret) > 4096 or any(c.isspace() for c in secret):
            raise ValueError()
    except (ValueError, ValidationError):
        raise HTTPException(422, "Invalid credential input") from None
    registry = request.app.state.services.stt
    try:
        await asyncio.to_thread(registry.credentials.save, secret)
    except Exception:
        raise HTTPException(503, "Credential Broker unavailable") from None
    finally:
        secret = None
        body = payload = None
    await registry.credential_changed()
    # Save-and-enable is explicit in the UI; broker configuration selects the
    # cloud provider with the requested defaults on first setup.
    return await registry.update(registry.config.model_copy(update={"provider": "deepgram"}))


@router.delete("/credential")
async def credential_remove(request: Request):
    require_loopback(request)
    registry = request.app.state.services.stt
    await asyncio.to_thread(registry.credentials.remove)
    await registry.credential_changed()
    return await registry.status()


@router.post("/probe")
async def probe(request: Request):
    require_loopback(request)
    registry = request.app.state.services.stt
    if registry.config.provider != "deepgram":
        raise HTTPException(409, "Select Deepgram to test its connection")
    if registry.active:
        raise HTTPException(409, "A microphone stream is active")

    async def ignore(event):
        pass

    provider = DeepgramSTTProvider(registry.config, AudioFormat(), registry.credentials, "connection-test", ignore)
    try:
        await provider.connect()
        registry.deepgram_state = STTState.READY
        registry.last_failure = None
        registry.retry_at = 0
        return {"auth": "PASS", "websocket": "PASS", "audio_sent": False}
    except STTFailure as failure:
        registry.remote_failed(failure)
        return {"auth": "NOT_TESTED_NO_CREDENTIAL" if failure.state == STTState.NOT_CONFIGURED else "FAIL",
                "websocket": "FAIL", "state": failure.state, "message": failure.code}
    finally:
        await provider.close()


@router.get("/benchmark/phrases")
async def benchmark_phrases():
    return {"phrases": PHRASES}


@router.post("/ticket")
async def stream_ticket(request: Request, payload: StreamRequest):
    require_loopback(request)
    services = request.app.state.services
    if payload.mode == "listening":
        allowed, reason = services.listening.can_process(payload.client_id)
        if not allowed:
            raise HTTPException(409, reason)
    if payload.benchmark and payload.mode != "diagnostic":
        raise HTTPException(422, "Benchmark requires diagnostic mode")
    try:
        return {"ticket": services.stt.issue_ticket(payload.model_dump(mode="json")), "expires_in": 15}
    except STTFailure as failure:
        raise HTTPException(409, failure.code) from None


@router.websocket("/stream")
async def stream(websocket: WebSocket):
    try:
        require_loopback(websocket)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    services = websocket.app.state.services
    session = None
    completion = disconnect = None
    send_lock = asyncio.Lock()

    async def send(payload):
        async with send_lock:
            await asyncio.wait_for(websocket.send_json(payload), 3)

    def allowed(payload: StreamRequest):
        if payload.mode == "listening":
            ok, reason = services.listening.can_process(payload.client_id)
            if not ok:
                raise STTFailure(STTState.ERROR, reason)

    try:
        handshake = await asyncio.wait_for(websocket.receive_text(), 5)
        if len(handshake) > 2048:
            raise ValueError()
        ticket = json.loads(handshake).get("ticket", "")
        payload = StreamRequest.model_validate(services.stt.consume_ticket(ticket))
        allowed(payload)
        if payload.mode == "listening" and services.settings.natural_conversation_enabled and services.stt.active:
            # The same capture can buffer a new utterance while the local STT
            # finishes the previous one. Still exactly one provider worker.
            await asyncio.wait_for(services.stt.available.wait(), 2.0)
            allowed(payload)
        session = await services.stt.open_session(payload.audio_format, send, mic_started_at=payload.mic_started_at)
        await send({"type": "ready", "utterance_id": session.utterance_id,
                    "utterance_end_ms": session.config.utterance_end_ms if session.config.provider == "deepgram"
                    and session.config.interim_results and services.stt.credentials.configured() else 0})
        deadline = time.monotonic() + 70
        while True:
            message = await asyncio.wait_for(websocket.receive(), min(10, max(.01, deadline - time.monotonic())))
            if message["type"] == "websocket.disconnect":
                return
            allowed(payload)
            if message.get("bytes") is not None:
                await session.send_audio(message["bytes"])
            else:
                raw = message.get("text", "")
                if len(raw) > 1024:
                    raise ValueError()
                control = json.loads(raw)
                if control.get("type") == "cancel":
                    return
                if control.get("type") == "end":
                    break
                raise ValueError()

        async def complete():
            transcript = await session.finish()
            diagnostic = session.diagnostics()
            comparison = None
            if payload.benchmark:
                local = transcript.text if transcript.provider == "faster_whisper" else (
                    await services.stt.local_engine.transcribe_pcm(bytes(session.audio), payload.audio_format.sample_rate)
                ).text
                comparison = {"audio": "SAME SAMPLE", "reference": payload.reference,
                              "deepgram": transcript.text if transcript.provider == "deepgram" else None,
                              "faster_whisper": local,
                              "deepgram_score": score(payload.reference, transcript.text) if transcript.provider == "deepgram" else None,
                              "faster_whisper_score": score(payload.reference, local)}
            # Release remote resources and the bounded audio before processing
            # the ordinary text turn. Never pass an interim to the conversation.
            await session.close()
            if payload.mode == "diagnostic":
                result = {"accepted": bool(transcript.text), "transcription": transcript.model_dump(mode="json"),
                          "diagnostics": diagnostic, "comparison": comparison}
            elif payload.mode == "listening":
                result = await services.conversation.listening_audio_turn(
                    None, payload.client_id, transcription=transcript, speech_end=time.perf_counter())
            else:
                result = await services.conversation.direct_audio_turn(
                    None, transcription=transcript, speech_end=time.perf_counter())
            await send({"type": "result", "result": result})

        completion = asyncio.create_task(complete(), name="kazumi-stt-complete")
        disconnect = asyncio.create_task(websocket.receive(), name="kazumi-stt-disconnect")
        done, _ = await asyncio.wait([completion, disconnect], timeout=180, return_when=asyncio.FIRST_COMPLETED)
        if completion in done:
            await completion
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Do not echo provider errors, request bodies, URLs or credentials.
        with suppress(Exception):
            await send({"type": "error", "message": exc.code if isinstance(exc, STTFailure) else "Recognition stream interrupted"})
    finally:
        for task in (completion, disconnect):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (completion, disconnect) if task), return_exceptions=True)
        if session:
            await session.close()
        with suppress(Exception):
            await websocket.close()
