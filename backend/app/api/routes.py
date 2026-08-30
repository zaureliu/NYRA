from __future__ import annotations

import asyncio
import json
import re
import time
import wave
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from app.integrations.base import IntegrationError
from pydantic import BaseModel, Field, ValidationError
from typing import Any

import logging

logger = logging.getLogger("nyra.routes")

from app.api.models import (
    ChatRequest,
    DesktopOpenRequest,
    ImportanceUpdate,
    ToolExecutionRequest,
    VoiceLabRequest,
    VoiceProfileUpdate,
    PronunciationPreviewRequest,
    PronunciationRuleRequest,
    AdultModeRequest,
    ListeningLeaseRequest,
    ListeningSettingsUpdate,
    PlaybackStateRequest,
    NetworkDebugRequest,
    NetworkWatchSettingsUpdate,
    SentinelDebugRequest,
    SentinelSettingsUpdate,
    SentinelTokenUpdate,
    VoiceHunterPreviewRequest,
    VoiceHunterCompareRequest,
    VoiceHunterPreferenceRequest,
    RealtimeSettingsUpdate,
    VoiceProcessorConfig,
    VoiceProcessorRequest,
    SkillExecutionRequest,
    SkillSettingsUpdate,
    RuntimeActionRequest,
    BrainBenchmarkRequest,
    BrainSelectionRequest,
    VTSSettingsUpdate,
    Live2DLipSyncRequest,
    Live2DCursorRequest,
    VTSPresenceReport,
    ShellApprovalDecision,
    AudioSettingsUpdate,
    InterruptionRequest,
)
from app.core.paths import DATA_ROOT, IDENTITY_ROOT
from app.core.runtime_settings import save_runtime_settings
from app.core.turn import PipelineFailure, TurnContext, TurnError
from app.core.capabilities import get_capabilities, set_capability
from app.core.settings_registry import (
    get_settings_v3,
    update_setting,
    export_config,
    SettingValidationError,
)
from app.core.release_info import (
    about_payload,
    release_health,
    support_bundle,
    world_state_snapshot,
)
from app.integrations.center import (
    integrations_status,
    integration_action,
    IntegrationActionError,
)
from app.integrations.home_assistant_profiles import (
    list_profiles,
    upsert_profile,
    remove_profile,
    activate_profile,
    set_profile_token,
    test_profile,
)
from app.integrations.openwrt import config as openwrt_ui_config
from app.integrations.proxmox import config as proxmox_ui_config
from app.speech.external_bridge import VoiceProcessorBridge
from app.speech.queue import SpeechPriority
from app.events import Event, EventType
from app.memory.models import MemoryCategory, MemoryCreate
from app.operator.monitoring import MonitorCreateRequest
from app.speech.profile import load_voice_profile
from app.speech.prosody import ProsodyProcessor
from app.speech.pronunciation.engine import PronunciationEngine, reload_engine
from app.speech.pronunciation.lexicon import load_dictionary, save_override, reset_override, OVERRIDE_PATH
from app.speech.reference import REFERENCE_PATH, inspect_reference, normalize_reference, RECOMMENDATIONS


router = APIRouter(prefix="/api")
SAFE_AUDIO = re.compile(
    r"^(?:nyra(?:-edge|-processed)?-[a-f0-9]+|voice-cache-[a-f0-9]{64})\.(?:wav|mp3)$"
)
from app.speech.tts_identity import NYRA_IDENTITY_ID, NYRA_VOICE_ID
PLAYBACK_CONFIRMATION_SECONDS = 12.0


def _voice_satellite_event_allowed(event: Event, satellite_id: str | None) -> bool:
    """Do not turn unrelated backend failures into blocking Satellite errors."""
    if not satellite_id or event.type != EventType.ERROR:
        return True
    owner = str(event.payload.get("satellite_id") or "")
    return bool(event.payload.get("satellite_action_required")) and owner == satellite_id


class UsbDeviceUpdateRequest(BaseModel):
    friendly_name: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=80)
    trusted: bool | None = None
    note: str | None = Field(default=None, max_length=240)
    registered: bool | None = None


@router.get("/health")
async def health(request: Request) -> dict:
    services = request.app.state.services
    llm_ok, llm_ready, memory_ok, stt_ok, tts_ok = await asyncio.gather(
        services.llm.health(),
        services.llm.ready(),
        services.memory.health(),
        services.stt.health(),
        services.tts.health(),
    )
    return {
        "status": "online" if llm_ok and memory_ok else "degraded",
        "character": "NYRA",
        "llm": llm_ok,
        "llm_ready": llm_ready,
        "ollama": services.warm_manager.status() if services.warm_manager else None,
        "memory": memory_ok,
        "stt": stt_ok,
        "tts": tts_ok,
        "pronunciation_engine": True,
        "always_listening": services.listening.enabled,
        "microphone": services.listening.status()["microphone"],
        "wake_word": services.listening.wake_word.name,
        "network_watch": services.network_watch.enabled,
        "system_shell": services.shell.status(),
        "remote_shell": services.remote_shell.status(),
        "agent": services.agent.status(),
        "sentinel_watch": {
            "enabled": services.sentinel.settings.sentinel_watch_enabled,
            "state": services.sentinel.state.value.casefold(),
        },
        "providers": {
            "llm": services.llm.name,
            "stt": services.stt.name,
            "tts": services.tts.name,
        },
        "model": services.settings.llm_model,
        "realtime": services.conversation.state.value,
        "perception": services.perception.snapshot.enabled,
    }


# ------------------------------------------------------------------ USB V1

@router.get("/usb/status")
async def usb_status(request: Request) -> dict:
    return await request.app.state.services.usb.status_snapshot()


@router.get("/usb/devices")
async def usb_devices(request: Request, include_internal: bool = False) -> dict:
    return {"devices": await request.app.state.services.usb.devices(
        include_internal=include_internal
    )}


@router.get("/usb/devices/connected")
async def usb_connected_devices(request: Request, include_internal: bool = False) -> dict:
    return {"devices": await request.app.state.services.usb.connected(
        include_internal=include_internal
    )}


@router.get("/usb/devices/known")
async def usb_known_devices(request: Request) -> dict:
    return {"devices": await request.app.state.services.usb.known()}


@router.get("/usb/devices/{device_id}")
async def usb_device_detail(request: Request, device_id: str) -> dict:
    device = await request.app.state.services.usb.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Dispositivo USB não encontrado.")
    return device


@router.get("/usb/history")
async def usb_history(request: Request, limit: int = 200,
                      event_type: str | None = None) -> dict:
    return {"history": await request.app.state.services.usb.history(
        limit=max(1, min(1000, limit)), event_type=event_type
    )}


@router.post("/usb/refresh")
async def usb_refresh(request: Request) -> dict:
    return await request.app.state.services.usb.refresh(reason="api")


@router.put("/usb/devices/{device_id}")
async def usb_update_device(payload: UsbDeviceUpdateRequest, request: Request,
                            device_id: str) -> dict:
    try:
        return await request.app.state.services.usb.update_device(
            device_id, payload.model_dump(exclude_unset=True)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispositivo USB não encontrado.") from error


@router.delete("/usb/devices/{device_id}")
async def usb_forget_device(request: Request, device_id: str) -> dict:
    try:
        return await request.app.state.services.usb.forget_device(device_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Dispositivo USB não encontrado.") from error


@router.post("/chat")
async def chat(payload: ChatRequest, request: Request):
    services = request.app.state.services
    if not await services.llm.ready():
        # nyra-full §41 / nyra-7c: comandos universais locais (abrir/fechar
        # app, pastas, arquivos, plano canônico do bloco de notas e skills
        # aprendidas) rodam no fast path SEM LLM e não ficam bloqueados pelo
        # warmup.
        from app.desktop.intents import parse_notepad_multistep, parse_universal_intent

        desktop = getattr(services, "desktop", None)
        computer = getattr(services, "computer", None)
        intent = (
            computer.can_handle_without_llm(
                payload.message, conversation_id="default",
                turn_id=payload.turn_id or None, channel="text")
            if computer is not None else None
        )
        if not intent:
            intent = parse_universal_intent(payload.message) if desktop is not None else None
        if intent is None and desktop is not None:
            intent = parse_notepad_multistep(payload.message)
        if intent is None:
            usb = getattr(services, "usb", None)
            if usb is not None and usb.can_handle_chat(payload.message):
                intent = True
        if intent is None:
            skill_memory = getattr(services, "skill_memory", None)
            usage = getattr(services, "usage_learning", None)
            has_skill_route = bool(skill_memory and skill_memory.match(payload.message))
            has_alias_route = False
            if not has_skill_route and usage is not None:
                words = payload.message.casefold().split()
                has_alias_route = bool(words and usage.resolve_alias(" ".join(words[:3])))
                has_alias_route = has_alias_route or (
                    payload.message.casefold().strip()
                    in {a.alias for a in usage.aliases.values()})
            if has_skill_route or has_alias_route:
                intent = True  # sentinel: existe rota determinística pós-warmup
        if intent is None:
            raise HTTPException(
                status_code=503,
                detail="IA local inicializando. Aguarde o warmup automático.",
            )
    turn = TurnContext(
        payload.message,
        conversation_id="default",
        turn_id=payload.turn_id or None,
    )
    try:
        return await services.orchestrator.converse(
            payload.message, synthesize=payload.synthesize, turn=turn
        )
    except PipelineFailure as exc:
        error = exc.error
        await services.event_bus.publish(EventType.ERROR, operation="chat", **error.model_dump())
        status = 409 if error.error_code == "TURN_SUPERSEDED" else 502
        raise HTTPException(status_code=status, detail=error.model_dump()) from exc
    except Exception as exc:
        error = TurnError(
            stage="pipeline", error_code="PIPELINE_FAILURE",
            exception_type=type(exc).__name__, message=str(exc)[:300],
            recoverable=True, turn_id=turn.turn_id,
        )
        await services.event_bus.publish(EventType.ERROR, operation="chat", **error.model_dump())
        raise HTTPException(status_code=502, detail=error.model_dump()) from exc


@router.get("/realtime/settings")
async def realtime_settings(request: Request):
    return request.app.state.services.v4_settings.value.model_dump(mode="json")


@router.put("/realtime/settings")
async def update_realtime_settings(payload: RealtimeSettingsUpdate, request: Request):
    services = request.app.state.services
    services.v4_settings.update_realtime(payload)
    services.reactions.config = payload.realtime
    # The Conversation Engine is the authority for interruption.  The legacy
    # realtime endpoint remains functional for perception tuning, but it no
    # longer toggles a second audio pipeline or the retired Voice Processor.
    allow_interruption = bool(
        payload.realtime.barge_in and payload.realtime.duplex_mode.value == "SMART_DUPLEX"
    )
    services.settings.voice_barge_in = allow_interruption
    await services.listening.update(
        services.listening.config().model_copy(update={"barge_in": allow_interruption})
    )
    await services.perception.update(payload.realtime, payload.privacy)
    return services.v4_settings.value.model_dump(mode="json")


@router.get("/realtime/status")
async def realtime_status(request: Request):
    services = request.app.state.services
    return {
        **services.orchestrator.debug_status(),
        "attention": services.attention.status(),
        "reaction": services.reactions.status(),
        "proactive": services.proactive.status(),
        "avatar": services.avatar.status(),
    }


@router.get("/conversation/status")
async def conversation_status(request: Request):
    return request.app.state.services.conversation.status()


@router.get("/audio/settings")
async def audio_settings(request: Request):
    services = request.app.state.services
    return {
        "settings": services.conversation.audio_settings(),
        "status": services.conversation.status(),
        "voices": [{
            "id": NYRA_VOICE_ID,
            "name": "Ava Multilingual Neural",
            "language": "pt-BR",
            "provider": "edge_tts",
        }],
    }


@router.put("/audio/settings")
async def update_audio_settings(payload: AudioSettingsUpdate, request: Request):
    return await request.app.state.services.conversation.update_audio_settings(payload)


@router.post("/audio/test-voice")
async def test_runtime_voice(request: Request):
    services = request.app.state.services
    text = "Olá. Este é um teste da voz atual da NYRA."
    prepared = ProsodyProcessor().prepare(text, provider=services.tts.name)
    started = time.perf_counter()
    response_id = f"turn_{uuid4().hex}"
    playback_started = asyncio.Event()
    playback_started_at: float | None = None

    async def confirm_playback(event: Event) -> None:
        nonlocal playback_started_at
        if (
            event.type == EventType.PLAYBACK_STARTED
            and str(event.payload.get("response_id") or "") == response_id
        ):
            playback_started_at = time.perf_counter()
            playback_started.set()

    await services.event_bus.subscribe(confirm_playback)
    services.telemetry.start(response_id, speech_end=started)
    services.telemetry.mark(response_id, "t_tts_start")
    try:
        await services.event_bus.publish(
            EventType.USER_TEXT_RECEIVED,
            response_id=response_id,
            turn_id=response_id,
            source="voice_test",
        )
        await services.event_bus.publish(
            EventType.TTS_STARTED,
            response_id=response_id,
            turn_id=response_id,
            state="neutral",
            source="voice_test",
            streaming=False,
        )
        output = await services.speech_queue.synthesize(
            services.tts,
            prepared.speech_text,
            "neutral",
            SpeechPriority.USER,
            response_id=response_id,
            turn_id=response_id,
        )
        services.telemetry.mark(response_id, "t_first_audio")
        services.telemetry.mark(response_id, "t_response_complete")
        finished_metrics = services.telemetry.finish(response_id)
        audio_url = f"/api/audio/{output.name}"
        await services.event_bus.publish(
            EventType.TTS_FINISHED,
            response_id=response_id,
            turn_id=response_id,
            state="neutral",
            audio_url=audio_url,
            source="voice_test",
            streaming=False,
        )
        try:
            await asyncio.wait_for(playback_started.wait(), PLAYBACK_CONFIRMATION_SECONDS)
        except TimeoutError as exc:
            await services.event_bus.publish(
                EventType.TTS_FAILED,
                response_id=response_id,
                turn_id=response_id,
                reason="playback_not_confirmed",
                source="voice_test",
            )
            raise HTTPException(
                status_code=504,
                detail="Áudio gerado, mas nenhum player confirmou o playback",
            ) from exc
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        if services.telemetry.active(response_id):
            services.telemetry.mark(response_id, "t_response_complete")
            services.telemetry.finish(response_id)
        logger.exception(
            "voice_test_failed stage=TTS exception_type=%s message=%s",
            type(exc).__name__,
            str(exc),
        )
        await services.event_bus.publish(
            EventType.TTS_FAILED,
            response_id=response_id,
            turn_id=response_id,
            reason=type(exc).__name__,
            source="voice_test",
        )
        raise HTTPException(status_code=502, detail=f"Falha no TTS atual: {type(exc).__name__}") from exc
    finally:
        await services.event_bus.unsubscribe(confirm_playback)
    metrics = dict(services.telemetry.last_metrics)
    if metrics.get("response_id") != response_id:
        metrics = dict(finished_metrics)
    if metrics.get("playback_start_ms") is None and playback_started_at is not None:
        metrics["playback_start_ms"] = round((playback_started_at - started) * 1000, 1)
    return {
        "audio_url": audio_url,
        "provider": services.tts.name,
        "primary_engine": services.tts.engine_id,
        "active_engine": services.tts.active_engine,
        "voice": services.tts.active_voice,
        "fallback_active": services.tts.fallback_active,
        "fallback_reason": services.tts.fallback_reason,
        "synthesis_ms": metrics.get("tts_first_audio_ms") or round((time.perf_counter() - started) * 1000, 1),
        "response_id": response_id,
        "playback_started": metrics.get("playback_start_ms") is not None,
        "playback_start_ms": metrics.get("playback_start_ms"),
    }


@router.post("/conversation/interrupt")
async def conversation_interrupt(payload: InterruptionRequest, request: Request):
    return await request.app.state.services.conversation.interrupt(payload.target, "api_or_voice")


@router.post("/conversation/speech-start")
async def conversation_speech_start(request: Request):
    interrupted = await request.app.state.services.conversation.speech_started("push_to_talk")
    return {"accepted": True, "interrupted": interrupted}


@router.get("/ollama/readiness")
async def ollama_readiness(request: Request):
    manager = request.app.state.services.warm_manager
    return manager.status() if manager else {"state": "OLLAMA_OFFLINE", "ready": False}


@router.post("/ollama/preload")
async def ollama_preload(request: Request):
    manager = request.app.state.services.warm_manager
    if manager is None:
        raise HTTPException(status_code=409, detail="O provider atual não é Ollama")
    return await manager.preload(force=True)


@router.post("/realtime/interrupt")
async def realtime_interrupt(request: Request):
    result = await request.app.state.services.conversation.interrupt(
        InterruptionRequest().target,
        "legacy_api_barge_in",
    )
    return {"interrupted": result["speech_cancelled"], **result}


@router.get("/realtime/debug")
async def realtime_debug(request: Request):
    services = request.app.state.services
    return {
        **services.orchestrator.debug_status(),
        "telemetry": services.telemetry.snapshot(),
        "perception": services.perception.public_snapshot(),
        "attention": services.attention.status(),
        "reaction": services.reactions.status(),
        "avatar": services.avatar.status(),
        "active_skill": None,
    }


@router.get("/turns/metrics")
async def turn_metrics(request: Request):
    return services_turn_snapshot(request)


def services_turn_snapshot(request: Request) -> dict:
    services = request.app.state.services
    registry = getattr(services.orchestrator, "turns", None)
    return registry.snapshot() if registry else {"metrics": {}, "active": [], "recent": []}


@router.get("/perception/status")
async def perception_status(request: Request):
    return request.app.state.services.perception.public_snapshot()


@router.post("/perception/poll")
async def perception_poll(request: Request):
    services = request.app.state.services
    if not services.v4_settings.value.realtime.perception_enabled:
        return services.perception.public_snapshot()
    await services.perception.poll_once()
    return services.perception.public_snapshot()


@router.get("/skills")
async def skills(request: Request):
    return {"skills": request.app.state.services.skills.list()}


@router.patch("/skills/{skill_name}")
async def update_skill(skill_name: str, payload: SkillSettingsUpdate, request: Request):
    try:
        return request.app.state.services.skills.update(skill_name, enabled=payload.enabled, cooldown_seconds=payload.cooldown_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/skills/{skill_name}")
async def execute_skill(skill_name: str, payload: SkillExecutionRequest, request: Request):
    try:
        return await request.app.state.services.skills.execute(skill_name, payload.parameters, confirmed=payload.confirmed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/voice-processor/settings")
async def voice_processor_settings(request: Request):
    return request.app.state.services.voice_processor.config.model_dump(mode="json")


@router.put("/voice-processor/settings")
async def update_voice_processor(payload: VoiceProcessorConfig, request: Request):
    services = request.app.state.services
    services.voice_processor.update(payload)
    services.v4_settings.update_voice_processor(payload)
    return payload.model_dump(mode="json")


@router.post("/voice-processor/process")
async def process_voice(payload: VoiceProcessorRequest, request: Request):
    services = request.app.state.services
    filename = Path(payload.audio_url).name
    if not SAFE_AUDIO.fullmatch(filename):
        raise HTTPException(status_code=422, detail="Arquivo de áudio não permitido")
    source = DATA_ROOT / "audio" / filename
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Áudio não encontrado")
    raw_metrics = await asyncio.to_thread(services.voice_processor.analyze, source)
    output = await services.voice_processor.process(source, payload.state)
    processed_metrics = await asyncio.to_thread(services.voice_processor.analyze, output)
    return {
        "raw_audio_url": f"/api/audio/{source.name}",
        "processed_audio_url": f"/api/audio/{output.name}",
        "raw": raw_metrics, "processed": processed_metrics,
    }


@router.get("/avatar/controller")
async def avatar_controller(request: Request):
    services = request.app.state.services
    return {"state": services.avatar.status(), "providers": {"current": True, "vtube_studio": services.vtube_studio.readiness()}}


@router.get("/live2d/settings")
async def live2d_settings(request: Request):
    return request.app.state.services.vtube_studio.readiness()


@router.put("/live2d/settings")
async def live2d_update(payload: VTSSettingsUpdate, request: Request):
    provider=request.app.state.services.vtube_studio; await provider.disconnect(); provider.update(payload)
    if payload.enabled and payload.auto_connect: await provider.connect(False)
    return provider.readiness()


@router.post("/live2d/connect")
async def live2d_connect(request: Request):
    return await request.app.state.services.vtube_studio.connect(False)


@router.post("/live2d/authorize")
async def live2d_authorize(request: Request):
    return await request.app.state.services.vtube_studio.authorize()


@router.post("/live2d/disconnect")
async def live2d_disconnect(request: Request):
    provider=request.app.state.services.vtube_studio; await provider.disconnect(); return provider.status()


@router.post("/live2d/lip-sync")
async def live2d_lip_sync(payload: Live2DLipSyncRequest, request: Request):
    await request.app.state.services.avatar.update(mouth_open=payload.value)
    return {"mouth_open": payload.value}


@router.post("/live2d/presence-status")
async def live2d_presence_status(payload: VTSPresenceReport, request: Request):
    return request.app.state.services.vtube_studio.record_presence(payload.model_dump(mode="json"))


@router.post("/live2d/cursor")
async def live2d_cursor(payload: Live2DCursorRequest, request: Request):
    services = request.app.state.services
    applied = await services.vtube_studio.apply_cursor(services.avatar.state, payload.x, payload.y)
    return {"applied": applied, "x": payload.x, "y": payload.y}


@router.post("/live2d/test/{mode}")
async def live2d_test(mode: str, request: Request):
    allowed={"neutral":"neutral","happy":"happy","curious":"curious","focused":"focused","concerned":"concerned","amused":"amused","surprised":"surprised","thinking":"focused","attention":"curious","head_tilt":"curious","nod":"neutral"}
    if mode not in allowed: raise HTTPException(status_code=422,detail="Teste Live2D inválido")
    avatar=request.app.state.services.avatar
    if mode=="thinking": await avatar.mode("thinking",allowed[mode])
    elif mode=="attention": await avatar.mode("listening",allowed[mode])
    else: await avatar.update(expression=allowed[mode],head_tilt=.16 if mode=="head_tilt" else 0)
    return avatar.status()


@router.get("/brain/models")
async def brain_models(request: Request):
    return await request.app.state.services.brain.inventory()


@router.post("/brain/use-temporarily")
async def brain_use_temporarily(payload: BrainSelectionRequest, request: Request):
    services = request.app.state.services
    _guard_model_operation(request)
    if not await services.brain.is_installed(payload.model):
        raise HTTPException(status_code=422, detail={
            "error_code": "MODEL_NOT_INSTALLED",
            "message": f"Modelo '{payload.model}' não está instalado no Ollama local.",
            "stage": "llm", "recoverable": True,
        })
    try:
        services.brain.use_temporarily(payload.model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if services.warm_manager:
        await services.warm_manager.request_rewarm()
    return await services.brain.inventory()


async def _validated_selection(payload: BrainSelectionRequest, request: Request):
    """Validações compartilhadas por salvar/carregar/restaurar modelo."""
    services = request.app.state.services
    _guard_model_operation(request)
    if not await services.brain.is_installed(payload.model):
        raise HTTPException(status_code=422, detail={
            "error_code": "MODEL_NOT_INSTALLED",
            "message": f"Modelo '{payload.model}' não está instalado no Ollama local.",
            "stage": "llm", "recoverable": True,
        })
    return services


def _guard_model_operation(request: Request) -> None:
    """Concorrência §16: uma troca por vez e nunca no meio de um turno."""
    app_state = request.app.state
    lock = getattr(app_state, "model_op_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state.model_op_lock = lock
    if lock.locked():
        raise HTTPException(status_code=409, detail={
            "error_code": "MODEL_CHANGE_IN_PROGRESS",
            "message": "Já existe uma troca de modelo em andamento.",
            "stage": "llm", "recoverable": True,
        })
    conversation = getattr(app_state.services, "conversation", None)
    busy = str(getattr(conversation, "state", "")) in {
        "USER_SPEAKING", "TRANSCRIBING", "THINKING", "TOOL_EXECUTION", "SPEAKING",
    }
    if busy:
        raise HTTPException(status_code=409, detail={
            "error_code": "TURN_ACTIVE",
            "message": "Existe um turno de conversa em andamento; tente novamente ao concluir.",
            "stage": "llm", "recoverable": True,
        })


async def _load_and_confirm(services, model: str) -> dict:
    """Cadeia real §8/§10: preload → warm-up isolado → residency /api/ps."""
    brain = services.brain
    brain.use_temporarily(model)
    if services.warm_manager:
        warm = await services.warm_manager.preload(force=True)
    else:
        await brain.warmup()
        warm = {"state": "OLLAMA_READY" if await brain.ready() else "OLLAMA_ERROR",
                "ready": await brain.ready(), "model": model}
    if str(warm.get("state")) == "OLLAMA_OFFLINE":
        raise HTTPException(status_code=503, detail={
            "error_code": "OLLAMA_OFFLINE",
            "message": "Ollama indisponível durante o carregamento do modelo.",
            "stage": "llm", "recoverable": True,
        })
    if not warm.get("ready") or not await brain.ready():
        raise HTTPException(status_code=502, detail={
            "error_code": "MODEL_NOT_RESIDENT",
            "message": f"Modelo '{model}' concluiu o preload mas não ficou residente.",
            "stage": "llm", "recoverable": True,
            "details": {"metrics": warm.get("metrics"), "last_error": warm.get("last_error")},
        })
    return {"state": "MODEL_READY", "active_model": model,
            "resident": True, "warm": warm}


@router.post("/brain/model/load")
async def brain_model_load(payload: BrainSelectionRequest, request: Request):
    """§8 Carregar modelo: valida existência real, aplica no runtime e confirma."""
    services = await _validated_selection(payload, request)
    try:
        return await _load_and_confirm(services, payload.model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/brain/status")
async def brain_status(request: Request):
    """Estados reais §4 para a seção IA da UI."""
    services = request.app.state.services
    brain = services.brain
    inventory = await brain.inventory()
    warm = services.warm_manager.status() if services.warm_manager else None
    active_model = inventory.get("active_model")
    if inventory.get("ollama_state") == "OFFLINE":
        state = "OLLAMA_OFFLINE"
    elif not inventory["ollama_ready"]:
        state = "OLLAMA_ERROR"
    elif inventory.get("inventory_error_code"):
        state = "OLLAMA_ERROR"
    elif not inventory["models"]:
        state = "NO_MODELS_INSTALLED"
    elif warm and warm.get("state") == "OLLAMA_LOADING":
        state = "MODEL_LOADING"
    elif active_model:
        state = "MODEL_READY"
    elif warm and warm.get("state") == "OLLAMA_ERROR":
        state = "MODEL_FAILED"
    else:
        state = "MODEL_AVAILABLE"
    return {
        "state": state,
        "ollama_ready": inventory["ollama_ready"],
        "ollama_state": inventory.get("ollama_state"),
        "active_model": active_model,
        "selected_model": brain.official_model,
        "resident_models": inventory.get("resident_models", []),
        "residency_known": inventory.get("residency_known", False),
        "configured_model_not_installed": inventory["configured_model_not_installed"],
        "models_installed": len(inventory["models"]),
        "error_code": inventory.get("inventory_error_code") or inventory.get("residency_error_code"),
        "last_error": (warm or {}).get("last_error"),
        "metrics": (warm or {}).get("metrics", {}),
    }


@router.post("/brain/reset-default")
async def brain_reset_default(request: Request):
    """§13 Restaurar padrão oficial (qwen3:8b): salvar → carregar → confirmar."""
    default_model = "qwen3:8b"
    services = request.app.state.services
    _guard_model_operation(request)
    if not await services.brain.is_installed(default_model):
        raise HTTPException(status_code=422, detail={
            "error_code": "DEFAULT_MODEL_NOT_INSTALLED",
            "message": f"O padrão oficial '{default_model}' não está instalado no Ollama local.",
            "stage": "llm", "recoverable": True,
        })
    try:
        services.brain.select_official(default_model, confirmed=True)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.settings.llm_model = default_model
    return await _load_and_confirm(services, default_model)


@router.post("/brain/select")
async def brain_select(payload: BrainSelectionRequest, request: Request):
    services = await _validated_selection(payload, request)
    try:
        services.brain.select_official(payload.model, payload.confirmed)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    services.settings.llm_model = payload.model
    if services.warm_manager:
        await services.warm_manager.request_rewarm()
    return await services.brain.inventory()


@router.post("/brain/restore")
async def brain_restore(request: Request):
    services = request.app.state.services
    services.brain.restore_official()
    if services.warm_manager:
        await services.warm_manager.request_rewarm()
    return await services.brain.inventory()


@router.post("/brain/warmup")
async def brain_warmup(request: Request):
    services = request.app.state.services
    return await services.warm_manager.preload(force=True) if services.warm_manager else await services.brain.warmup()


@router.post("/brain/benchmark")
async def brain_benchmark(payload: BrainBenchmarkRequest, request: Request):
    services = request.app.state.services
    installed = {item["name"] for item in (await services.brain.inventory())["models"] if item["installed"]}
    if any(model not in installed for model in payload.models):
        raise HTTPException(status_code=422, detail="Todos os modelos do benchmark precisam estar instalados")
    return await services.brain.benchmark(payload.models, payload.context_size)


@router.get("/state")
async def state(request: Request) -> dict:
    current = await request.app.state.services.state_machine.current()
    return {"state": current.value, "status": "IDLE"}


@router.get("/memory")
async def list_memory(
    request: Request,
    category: MemoryCategory = MemoryCategory.EPISODIC,
    limit: int = 50,
    offset: int = 0,
):
    return await request.app.state.services.memory.list(category, limit, offset)


@router.get("/memory/search")
async def search_memory(request: Request, q: str, limit: int = 8):
    if not q.strip():
        return []
    return await request.app.state.services.memory.search(q, limit)


@router.post("/memory", status_code=201)
async def create_memory(payload: MemoryCreate, request: Request):
    if payload.category == MemoryCategory.SHORT_TERM and not payload.role:
        raise HTTPException(status_code=422, detail="role é obrigatório para short_term")
    return await request.app.state.services.memory.add(payload)


@router.delete("/memory/{category}/{memory_id}", status_code=204)
async def delete_memory(category: MemoryCategory, memory_id: int, request: Request):
    deleted = await request.app.state.services.memory.delete(category, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memória não encontrada")


@router.patch("/memory/{category}/{memory_id}/importance")
async def update_importance(
    category: MemoryCategory, memory_id: int, payload: ImportanceUpdate, request: Request
):
    updated = await request.app.state.services.memory.set_importance(
        category, memory_id, payload.importance
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Memória não encontrada")
    return {"updated": True, "importance": payload.importance}


@router.get("/tools")
async def list_tools(request: Request):
    return request.app.state.services.tools.descriptions()


@router.post("/tools/{tool_name}")
async def execute_tool(tool_name: str, payload: ToolExecutionRequest, request: Request):
    try:
        return await request.app.state.services.tools.execute(
            tool_name, payload.parameters, exposure="api",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/shell/status")
async def shell_status(request: Request):
    return request.app.state.services.shell.status()


@router.get("/shell/history")
async def shell_history(request: Request, limit: int = 50):
    return await request.app.state.services.shell.history.recent(limit)


@router.get("/shell/approvals")
async def shell_approvals(request: Request):
    return {"approvals": request.app.state.services.shell.approvals.pending()}


@router.post("/shell/approvals/{approval_id}")
async def decide_shell_approval(
    approval_id: str,
    payload: ShellApprovalDecision,
    request: Request,
):
    result = await request.app.state.services.shell.decide_approval(approval_id, payload.approved)
    if result is None:
        raise HTTPException(status_code=404, detail="Approval ID inexistente, expirado ou já decidido")
    if not payload.approved:
        await request.app.state.services.agent.approval_denied(result.get("agent_run_id"))
    return result


@router.get("/remote-shell/status")
async def remote_shell_status(request: Request):
    return request.app.state.services.remote_shell.status()


@router.get("/remote-shell/history")
async def remote_shell_history(request: Request, limit: int = 50):
    return await request.app.state.services.remote_shell.history.recent(limit)


@router.get("/agent/status")
async def agent_status(request: Request):
    return request.app.state.services.agent.status()


@router.get("/agent/runs")
async def agent_runs(request: Request, limit: int = 30):
    return {"runs": await request.app.state.services.agent.recent(limit)}


@router.get("/agent/runs/{run_id}")
async def agent_run(run_id: str, request: Request):
    result = await request.app.state.services.agent.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Agent Run não encontrado")
    return result


@router.post("/agent/runs/{run_id}/cancel")
async def cancel_agent_run(run_id: str, request: Request):
    if not await request.app.state.services.agent.cancel(run_id):
        raise HTTPException(status_code=409, detail="Agent Run inexistente ou já finalizado")
    return {"cancelled": True, "agent_run_id": run_id}


@router.post("/speech/transcribe")
async def transcribe(request: Request, audio: UploadFile = File(...)):
    allowed_types = {
        "audio/webm": ".webm",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "application/octet-stream": ".webm",
    }
    suffix = allowed_types.get(audio.content_type or "")
    if not suffix:
        raise HTTPException(status_code=415, detail="Formato de áudio não suportado")
    content = await audio.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Áudio excede 25 MB")
    path = DATA_ROOT / "recordings" / f"capture-{uuid4().hex}{suffix}"
    await asyncio.to_thread(path.write_bytes, content)
    try:
        await request.app.state.services.event_bus.publish(EventType.USER_SPEECH_RECEIVED)
        result = await request.app.state.services.stt.transcribe(path)
        return result
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Falha de transcrição: {type(exc).__name__}") from exc
    finally:
        path.unlink(missing_ok=True)


@router.post("/conversation/turn")
async def conversation_turn(request: Request, audio: UploadFile = File(...)):
    """End-to-end push-to-talk turn; empty STT output never reaches the LLM."""
    allowed_types = {
        "audio/webm": ".webm", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "application/octet-stream": ".webm",
    }
    suffix = allowed_types.get(audio.content_type or "")
    if not suffix:
        raise HTTPException(status_code=415, detail="Formato de áudio não suportado")
    content = await audio.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Áudio excede 25 MB")
    path = DATA_ROOT / "recordings" / f"turn-{uuid4().hex}{suffix}"
    await asyncio.to_thread(path.write_bytes, content)
    await request.app.state.services.event_bus.publish(EventType.USER_SPEECH_RECEIVED, source="push_to_talk")
    try:
        return await request.app.state.services.conversation.direct_audio_turn(
            path,
            speech_end=time.perf_counter(),
        )
    except Exception as exc:
        await request.app.state.services.event_bus.publish(
            EventType.ERROR, operation="conversation_turn", error=type(exc).__name__
        )
        raise HTTPException(status_code=422, detail=f"Falha no turno de voz: {type(exc).__name__}") from exc
    finally:
        path.unlink(missing_ok=True)


@router.get("/listening/settings")
async def listening_settings(request: Request):
    services = request.app.state.services
    return {"settings": services.listening.config().model_dump(mode="json"), "status": services.listening.status()}


@router.put("/listening/settings")
async def update_listening_settings(payload: ListeningSettingsUpdate, request: Request):
    return await request.app.state.services.listening.update(payload)


@router.get("/listening/status")
async def listening_status(request: Request):
    return request.app.state.services.listening.status()


@router.post("/listening/start")
async def start_listening(request: Request):
    return await request.app.state.services.listening.set_enabled(True)


@router.post("/listening/stop")
async def stop_listening(request: Request):
    return await request.app.state.services.listening.set_enabled(False)


@router.post("/listening/mute")
async def mute_listening(request: Request):
    return await request.app.state.services.listening.set_muted(True)


@router.post("/listening/unmute")
async def unmute_listening(request: Request):
    return await request.app.state.services.listening.set_muted(False)


@router.post("/listening/lease")
async def listening_lease(payload: ListeningLeaseRequest, request: Request):
    acquired = await request.app.state.services.listening.acquire_lease(payload.client_id)
    return {"acquired": acquired, "status": request.app.state.services.listening.status()}


@router.post("/listening/playback")
async def listening_playback(payload: PlaybackStateRequest, request: Request):
    services = request.app.state.services
    status = await services.listening.playback(payload.playing)
    if payload.playing and payload.response_id:
        services.telemetry.playback_started(payload.response_id)
        services.speech_queue.playback_started(payload.response_id)
        await services.event_bus.publish(EventType.PLAYBACK_STARTED, response_id=payload.response_id)
    return status


@router.post("/listening/speech-start")
async def listening_speech_start(payload: ListeningLeaseRequest, request: Request):
    services = request.app.state.services
    allowed, reason = services.listening.can_process(payload.client_id)
    if services.listening.speaking and services.settings.voice_barge_in:
        interrupted = await services.conversation.speech_started("always_listening")
        return {"accepted": True, "interrupted": interrupted}
    if allowed:
        await services.conversation.speech_started("always_listening")
    return {"accepted": allowed, "reason": reason, "interrupted": False}


@router.post("/listening/utterance")
async def listening_utterance(
    request: Request,
    client_id: str = Form(..., min_length=8, max_length=100),
    audio: UploadFile = File(...),
):
    services = request.app.state.services
    speech_end = time.perf_counter()
    allowed, reason = services.listening.can_process(client_id)
    if not allowed:
        return {"accepted": False, "reason": reason, "status": services.listening.status()}
    if not await services.llm.ready():
        return {
            "accepted": False,
            "reason": "ai_initializing",
            "status": services.listening.status(),
        }
    allowed_types = {
        "audio/webm": ".webm", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "application/octet-stream": ".webm",
    }
    suffix = allowed_types.get(audio.content_type or "")
    if not suffix:
        raise HTTPException(status_code=415, detail="Formato de áudio não suportado")
    content = await audio.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Áudio excede 25 MB")
    recordings = (DATA_ROOT / "recordings").resolve()
    path = recordings / f"always-{uuid4().hex}{suffix}"
    try:
        await asyncio.to_thread(recordings.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, content)
        await services.event_bus.publish(EventType.USER_SPEECH_RECEIVED, source="always_listening")
        return await services.conversation.listening_audio_turn(
            path,
            client_id,
            speech_end=speech_end,
        )
    except PipelineFailure as exc:
        # Full message/path/traceback are recorded by ConversationEngine. The
        # browser receives only a safe stage and exception type.
        raise HTTPException(
            status_code=422,
            detail=f"Falha na etapa {str(exc.error.stage).upper()}: {exc.error.exception_type}",
        ) from exc
    except Exception as exc:
        logger.exception(
            "always_listening_route_failed stage=UPLOAD exception_type=%s message=%s path=%s",
            type(exc).__name__,
            str(exc),
            str(path),
        )
        raise HTTPException(status_code=500, detail=f"Falha ao preparar áudio: {type(exc).__name__}") from exc
    finally:
        await asyncio.to_thread(path.unlink, missing_ok=True)


@router.get("/network-watch/settings")
async def network_watch_settings(request: Request):
    services = request.app.state.services
    return {"settings": services.network_watch.config().model_dump(mode="json"), "status": services.network_watch.status()}


@router.put("/network-watch/settings")
async def update_network_watch_settings(payload: NetworkWatchSettingsUpdate, request: Request):
    return await request.app.state.services.network_watch.update(payload)


@router.get("/network-watch/status")
async def network_watch_status(request: Request):
    return request.app.state.services.network_watch.status()


@router.post("/network-watch/start")
async def start_network_watch(request: Request):
    monitor = request.app.state.services.network_watch
    return await monitor.update(monitor.config().model_copy(update={"enabled": True}))


@router.post("/network-watch/stop")
async def stop_network_watch(request: Request):
    monitor = request.app.state.services.network_watch
    return await monitor.update(monitor.config().model_copy(update={"enabled": False}))


@router.post("/network-watch/poll")
async def poll_network_watch(request: Request):
    return await request.app.state.services.network_watch.poll_once(force=True)


@router.get("/network-watch/metrics")
async def network_watch_metrics(request: Request, minutes: int = 5):
    return {"minutes": minutes, "samples": request.app.state.services.network_watch.sample_window(minutes)}


@router.get("/network-watch/events")
async def network_watch_events(request: Request, hours: int = 24, limit: int = 100):
    return {"events": await request.app.state.services.network_watch.history.recent(hours, limit)}


@router.get("/network-watch/quality")
async def network_watch_quality(request: Request, hours: int = 1):
    monitor = request.app.state.services.network_watch
    return {**await monitor.history.summary(hours), "current": monitor.status()}


@router.post("/network-watch/debug")
async def network_watch_debug(payload: NetworkDebugRequest, request: Request):
    if request.app.state.services.settings.environment != "development":
        raise HTTPException(status_code=404, detail="Debug indisponível")
    event = await request.app.state.services.network_watch.inject(payload.event)
    return event.model_dump(mode="json")


@router.get("/sentinel-watch/settings")
async def sentinel_watch_settings(request: Request):
    connector = request.app.state.services.sentinel
    return {
        "settings": connector.config().model_dump(mode="json"),
        "status": connector.status(),
        "token_configured": connector.secrets.configured(),
    }


@router.put("/sentinel-watch/settings")
async def update_sentinel_watch_settings(payload: SentinelSettingsUpdate, request: Request):
    return await request.app.state.services.sentinel.update(payload)


@router.put("/sentinel-watch/token")
async def update_sentinel_watch_token(payload: SentinelTokenUpdate, request: Request):
    return await request.app.state.services.sentinel.set_token(payload.token)


@router.delete("/sentinel-watch/token")
async def clear_sentinel_watch_token(request: Request):
    return await request.app.state.services.sentinel.clear_token()


@router.get("/sentinel-watch/status")
async def sentinel_watch_status(request: Request):
    return request.app.state.services.sentinel.status()


@router.post("/sentinel-watch/start")
async def start_sentinel_watch(request: Request):
    connector = request.app.state.services.sentinel
    return await connector.update(connector.config().model_copy(update={"enabled": True}))


@router.post("/sentinel-watch/stop")
async def stop_sentinel_watch(request: Request):
    connector = request.app.state.services.sentinel
    return await connector.update(connector.config().model_copy(update={"enabled": False}))


@router.post("/sentinel-watch/discover")
async def discover_sentinel_now(request: Request):
    return await request.app.state.services.sentinel.find_now()


@router.post("/sentinel-watch/disconnect")
async def disconnect_sentinel(request: Request):
    return await request.app.state.services.sentinel.disconnect(reconnect=False)


@router.post("/sentinel-watch/reconnect")
async def reconnect_sentinel(request: Request):
    return await request.app.state.services.sentinel.reconnect()


@router.get("/sentinel-watch/test")
async def test_sentinel_connection(request: Request):
    return await request.app.state.services.sentinel.test_connection()


@router.delete("/sentinel-watch/saved-host")
async def clear_sentinel_saved_host(request: Request):
    return await request.app.state.services.sentinel.clear_saved_host()


@router.get("/sentinel-watch/events")
async def sentinel_events(request: Request, hours: int = 24, limit: int = 50, severity: str = ""):
    if severity not in {"", "info", "warning", "critical", "recovery"}:
        raise HTTPException(status_code=422, detail="Severity inválida")
    return {"events": await request.app.state.services.sentinel.history.recent(hours, limit, severity)}


@router.get("/sentinel-watch/summary")
async def sentinel_summary(request: Request, hours: int = 1):
    return await request.app.state.services.sentinel.summary(hours)


@router.post("/sentinel-watch/debug")
async def sentinel_debug(payload: SentinelDebugRequest, request: Request):
    try:
        return await request.app.state.services.sentinel.inject(payload.severity.value)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/audio/{filename}")
async def audio(filename: str):
    if not SAFE_AUDIO.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Áudio não encontrado")
    path = DATA_ROOT / "audio" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Áudio não encontrado")
    media_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
    return FileResponse(path, media_type=media_type, filename=f"nyra-response{path.suffix}")


@router.get("/homelab/status")
async def homelab_status(request: Request):
    services = request.app.state.services
    return {
        "poll_interval": services.settings.homelab_poll_interval,
        "proactive_mode": services.settings.proactive_mode,
        "last_stats": services.monitor.last_stats,
        "proxmox_configured": services.proxmox.configured,
        "openwrt_configured": bool(services.settings.openwrt_url),
        "enabled": services.settings.homelab_enabled,
        "configuration": services.homelab.configuration_status(),
        "hosts": services.homelab.list_hosts(),
    }


@router.post("/homelab/poll")
async def poll_homelab(request: Request):
    return await request.app.state.services.monitor.poll_once()


@router.get("/homelab/overview")
async def homelab_overview(request: Request, force: str = "false"):
    if not request.app.state.services.settings.homelab_enabled:
        raise HTTPException(status_code=503, detail="Homelab Control Plane está desabilitado.")
    overview = await request.app.state.services.homelab.overview(force=force == "true")
    return overview.model_dump(mode="json")


@router.get("/homelab/hosts/{host_id}")
async def homelab_host_detail(host_id: str, request: Request):
    try:
        health = await request.app.state.services.homelab.host_status(host_id)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code == "HOMELAB_HOST_UNKNOWN":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=f"Falha ao consultar host: {getattr(exc, 'message', type(exc).__name__)}") from exc
    return health.model_dump(mode="json")


@router.get("/homelab/proxmox/vms")
async def homelab_proxmox_vms(request: Request):
    try:
        guests = await request.app.state.services.homelab.proxmox_list_vms(include_lxc=True)
    except Exception as exc:
        code = getattr(exc, "code", "PROXMOX_API_UNAVAILABLE")
        raise HTTPException(status_code=502, detail={"error_code": code, "message": getattr(exc, "message", str(exc))}) from exc
    running = sum(1 for g in guests if g["status"] == "running")
    return {"count": len(guests), "running_count": running, "vms": guests}


@router.get("/homelab/proxmox/vms/{vm_reference}")
async def homelab_proxmox_vm(vm_reference: str, request: Request):
    try:
        guest = await request.app.state.services.homelab.proxmox_vm_status(vm_reference)
    except Exception as exc:
        code = getattr(exc, "code", None)
        status = 404 if code == "PROXMOX_VM_NOT_FOUND" else 502
        raise HTTPException(status_code=status, detail=getattr(exc, "message", str(exc))) from exc
    return guest


@router.get("/homelab/home-assistant/status")
async def homelab_ha_status(request: Request):
    result = await request.app.state.services.homelab.ha_status()
    if not result.get("success", True) or result.get("state") == "DISABLED":
        return result
    return result


@router.get("/homelab/history")
async def homelab_history(request: Request, limit: int = 30):
    rows = await request.app.state.services.homelab.history.recent(limit)
    return {"events": rows}


@router.get("/settings")
async def settings(request: Request):
    return request.app.state.services.settings.public_dict()


@router.get("/settings/adult-mode")
async def adult_mode_status(request: Request):
    return {"enabled": request.app.state.services.settings.adult_mode_enabled, "requires_confirmation": True, "scope": "mature_non_explicit"}


@router.put("/settings/adult-mode")
async def update_adult_mode(payload: AdultModeRequest, request: Request):
    if payload.enabled and not payload.confirmed_18_plus:
        raise HTTPException(status_code=400, detail="Confirmação de maioridade necessária")
    services = request.app.state.services
    services.settings.adult_mode_enabled = payload.enabled
    services.orchestrator.context.adult_mode = payload.enabled
    path = DATA_ROOT / "settings-adult.json"
    await asyncio.to_thread(path.write_text, json.dumps({"adult_mode_enabled": payload.enabled}) + "\n", "utf-8")
    return {"enabled": payload.enabled, "scope": "mature_non_explicit", "saved": True}


@router.get("/voice/providers")
async def voice_providers(request: Request):
    services = request.app.state.services
    show_all = request.query_params.get("all") == "true"
    health = await asyncio.gather(*(provider.health() for provider in services.tts_catalog))
    if show_all:
        for provider in services.tts_catalog:
            if provider.name == "edge_tts" and hasattr(provider, "refresh_voices"):
                await provider.refresh_voices(all_voices=True)
    return {
        "active": services.tts.name,
        "providers": [
            {
                "id": provider.name,
                "available": available,
                "voices": provider.voices,
                "supported_parameters": provider.supported_parameters,
                "model_id": getattr(provider, "model_id", None),
                "reference_available": bool(getattr(provider, "reference_path", None) and provider.reference_path.is_file()),
                "provider_type": provider.provider_type,
            }
            for provider, available in zip(services.tts_catalog, health, strict=True)
        ],
    }


@router.get("/voice-hunter/status")
async def voice_hunter_status(request: Request):
    hunter = request.app.state.services.voice_hunter
    return {
        "state": hunter.state.model_dump(mode="json"),
        "candidates": hunter.list_candidates(),
    }


@router.get("/voice-hunter/phrases")
async def voice_hunter_phrases():
    from app.voice_hunter.service import BENCHMARK_PHRASES
    return {"phrases": BENCHMARK_PHRASES}


@router.post("/voice-hunter/search", status_code=202)
async def voice_hunter_search(request: Request):
    state = await request.app.state.services.voice_hunter.start_search()
    return state.model_dump(mode="json")


@router.post("/voice-hunter/cancel")
async def voice_hunter_cancel(request: Request):
    state = await request.app.state.services.voice_hunter.cancel()
    return state.model_dump(mode="json")


@router.get("/voice-hunter/candidates/{candidate_id}/sample")
async def voice_hunter_sample(candidate_id: str, request: Request):
    try:
        path = request.app.state.services.voice_hunter.sample_path(candidate_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Sample não disponível")
    return FileResponse(path, media_type="audio/wav")


@router.post("/voice-hunter/candidates/{candidate_id}/preview")
async def voice_hunter_preview(candidate_id: str, payload: VoiceHunterPreviewRequest, request: Request):
    try:
        return await request.app.state.services.voice_hunter.preview(candidate_id, payload.phrase, payload.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidata não encontrada")
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="Candidata ainda não possui sample ou provider disponível")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/voice-hunter/compare")
async def voice_hunter_compare(payload: VoiceHunterCompareRequest, request: Request):
    if payload.candidate_a == payload.candidate_b:
        raise HTTPException(status_code=400, detail="Selecione duas candidatas diferentes")
    hunter = request.app.state.services.voice_hunter
    try:
        a, b = await asyncio.gather(
            hunter.preview(payload.candidate_a, payload.phrase),
            hunter.preview(payload.candidate_b, payload.phrase),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidata não encontrada")
    except (PermissionError, FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"a": a, "b": b, "phrase": payload.phrase}


@router.patch("/voice-hunter/candidates/{candidate_id}")
async def voice_hunter_preference(candidate_id: str, payload: VoiceHunterPreferenceRequest, request: Request):
    try:
        candidate = await request.app.state.services.voice_hunter.set_preference(
            candidate_id, favorite=payload.favorite, discarded=payload.discarded, rating=payload.rating,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidata não encontrada")
    return candidate.model_dump(mode="json")


@router.post("/voice-hunter/candidates/{candidate_id}/select")
async def voice_hunter_select(candidate_id: str, request: Request):
    services = request.app.state.services
    try:
        result = await services.voice_hunter.select_official(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidata não encontrada")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    provider = next((item for item in services.tts_catalog if item.name == result["provider"]), None)
    if provider is not None:
        services.tts = provider
        services.orchestrator.tts = provider
    return result


@router.delete("/voice-hunter/candidates/discarded")
async def voice_hunter_cleanup(request: Request):
    return await request.app.state.services.voice_hunter.cleanup_discarded()


@router.get("/pronunciation/lexicon")
async def pronunciation_lexicon(query: str = "", category: str = ""):
    needle = query.casefold().strip()
    dictionary = load_dictionary()
    rules = dictionary.rules
    return {"version": dictionary.version, "rules": [rule.model_dump(mode="json") for rule in rules if (not needle or needle in rule.canonical.casefold() or any(needle in alias.casefold() for alias in rule.aliases)) and (not category or rule.category == category)]}


@router.post("/pronunciation/preview")
async def pronunciation_preview(payload: PronunciationPreviewRequest):
    result = PronunciationEngine().prepare_for_speech(payload.text, payload.provider, literal_required=payload.literal_required)
    return result.model_dump(mode="json")


@router.post("/pronunciation/rules")
async def pronunciation_save_rule(payload: PronunciationRuleRequest):
    save_override(payload)
    reload_engine()
    return {"saved": True, "canonical": payload.canonical, "reloaded": True}


@router.delete("/pronunciation/rules/{canonical}")
async def pronunciation_reset_rule(canonical: str):
    reset_override(canonical)
    reload_engine()
    return {"reset": True, "canonical": canonical}


@router.get("/pronunciation/export")
async def pronunciation_export():
    return load_dictionary().model_dump(mode="json")


@router.post("/pronunciation/import")
async def pronunciation_import(payload: dict):
    from app.speech.pronunciation.models import PronunciationDictionary
    dictionary = PronunciationDictionary.model_validate(payload)
    for rule in dictionary.rules:
        save_override(rule)
    return {"imported": len(dictionary.rules), "version": dictionary.version}


@router.post("/voice/providers/refresh")
async def refresh_voice_providers(request: Request):
    services = request.app.state.services
    for provider in services.tts_catalog:
        if provider.name == "edge_tts" and hasattr(provider, "refresh_voices"):
            await provider.refresh_voices(all_voices=request.query_params.get("all") == "true")
            provider._health = bool(provider.voices)
    return await voice_providers(request)


@router.get("/voice/profile")
async def voice_profile():
    raw, options = load_voice_profile()
    return {"profile_id": raw.get("profile_id"), "model": raw.get("model"), "reference_file": raw.get("reference_file"), **options.model_dump()}


@router.get("/voice/reference")
async def voice_reference():
    return inspect_reference()


@router.post("/voice/reference")
async def import_voice_reference(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=415, detail="A referência deve ser um arquivo WAV")
    temp = DATA_ROOT / "voices" / f".upload-{uuid4().hex}.wav"
    temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = await file.read(50 * 1024 * 1024 + 1)
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Referência excede 50 MB")
        await asyncio.to_thread(temp.write_bytes, content)
        result = await asyncio.to_thread(normalize_reference, temp, REFERENCE_PATH)
        if not result.get("valid"):
            raise HTTPException(status_code=422, detail="WAV inválido ou sem áudio utilizável")
        return {**result, "recommendations": RECOMMENDATIONS, "stored": "data/voices/nyra_reference.wav"}
    finally:
        temp.unlink(missing_ok=True)


@router.post("/voice/synthesize")
async def voice_lab_synthesize(payload: VoiceLabRequest, request: Request):
    services = request.app.state.services
    provider = next((item for item in services.tts_catalog if item.name == payload.provider), None)
    if provider is None or not await provider.health():
        raise HTTPException(status_code=409, detail=f"Provider {payload.provider} indisponível neste host")
    prepared = ProsodyProcessor().prepare(payload.text, provider=payload.provider)
    started = time.perf_counter()
    try:
        output = await provider.synthesize(prepared.speech_text, payload.state, payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha no TTS: {type(exc).__name__}") from exc
    synthesis_ms = round((time.perf_counter() - started) * 1000, 1)
    duration_ms = None
    try:
        with wave.open(str(output), "rb") as audio_file:
            duration_ms = round(audio_file.getnframes() / audio_file.getframerate() * 1000, 1)
    except (wave.Error, OSError, ZeroDivisionError):
        pass
    return {
        "provider": provider.name,
        "model": getattr(provider, "model_id", None),
        "reference": bool(getattr(provider, "reference_path", None) and provider.reference_path.is_file()),
        "voice": payload.voice,
        "display_text": prepared.display_text,
        "speech_text": prepared.speech_text,
        "audio_url": f"/api/audio/{output.name}",
        "debug": {
            "synthesis_ms": synthesis_ms,
            "text_length": len(prepared.speech_text),
            "duration_ms": duration_ms,
            "fallback": False,
            "model": getattr(provider, "model_id", None),
            "pronunciation_rules": prepared.applied_rules,
        },
    }


@router.put("/voice/profile")
async def save_voice_profile(payload: VoiceProfileUpdate, request: Request):
    services = request.app.state.services
    provider = next((item for item in services.tts_catalog if item.name == payload.provider), None)
    if provider is None or not await provider.health():
        raise HTTPException(status_code=409, detail="Provider selecionado não está funcional")
    path = IDENTITY_ROOT / "voice_profile.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    current.update(payload.model_dump())
    current["profile_id"] = NYRA_IDENTITY_ID
    await asyncio.to_thread(
        path.write_text, json.dumps(current, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    services.tts = provider
    services.orchestrator.tts = provider
    return {"saved": True, "provider": provider.name, "voice": payload.voice}


@router.get("/microphone/config")
async def microphone_config(request: Request):
    value = request.app.state.services.settings
    return {
        "device": value.microphone,
        "gain": value.mic_gain,
        "preroll_ms": value.mic_preroll_ms,
        "postroll_ms": value.mic_postroll_ms,
        "vad": {
            "enabled": value.vad_enabled,
            "threshold": value.vad_threshold,
            "min_speech_ms": value.vad_min_speech_ms,
            "min_silence_ms": value.vad_min_silence_ms,
            "speech_pad_ms": value.vad_speech_pad_ms,
            "engine": "silero_v6_onnx",
        },
    }


@router.get("/runtime/services")
async def runtime_services(request: Request):
    supervisor = request.app.state.services.runtime_supervisor
    snapshots = await supervisor.inspect_all_public()
    return {"services": [snapshot.model_dump(mode="json") for snapshot in snapshots]}


@router.get("/runtime/services/{service_id}")
async def runtime_service_detail(request: Request, service_id: str):
    supervisor = request.app.state.services.runtime_supervisor
    if supervisor._snapshot(service_id) is None:
        raise HTTPException(status_code=404, detail="Serviço não registrado")
    snapshot = await supervisor.inspect(service_id)
    return snapshot.model_dump(mode="json")


@router.get("/runtime/services/{service_id}/health")
async def runtime_service_health(request: Request, service_id: str):
    supervisor = request.app.state.services.runtime_supervisor
    result = await supervisor.health(service_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error_code", "erro"))
    return result


@router.get("/runtime/services/{service_id}/logs")
async def runtime_service_logs(request: Request, service_id: str, lines: int = 100):
    supervisor = request.app.state.services.runtime_supervisor
    result = await supervisor.logs(service_id, lines=lines)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error_code", "erro"))
    return result


@router.post("/runtime/services/{service_id}/start")
async def runtime_service_start(request: Request, service_id: str, payload: RuntimeActionRequest | None = None):
    body = payload or RuntimeActionRequest()
    supervisor = request.app.state.services.runtime_supervisor
    from app.runtime.tools import _mutate
    return await _mutate(supervisor, request.app.state.services.shell.approvals,
                         supervisor.event_bus, "start", service_id, body.approval_id or None)


@router.post("/runtime/services/{service_id}/stop")
async def runtime_service_stop(request: Request, service_id: str, payload: RuntimeActionRequest | None = None):
    body = payload or RuntimeActionRequest()
    supervisor = request.app.state.services.runtime_supervisor
    from app.runtime.tools import _mutate
    return await _mutate(supervisor, request.app.state.services.shell.approvals,
                         supervisor.event_bus, "stop", service_id, body.approval_id or None)


@router.post("/runtime/services/{service_id}/restart")
async def runtime_service_restart(request: Request, service_id: str, payload: RuntimeActionRequest | None = None):
    body = payload or RuntimeActionRequest()
    supervisor = request.app.state.services.runtime_supervisor
    from app.runtime.tools import _mutate
    return await _mutate(supervisor, request.app.state.services.shell.approvals,
                         supervisor.event_bus, "restart", service_id, body.approval_id or None)


@router.get("/runtime/history")
async def runtime_history(request: Request, limit: int = 50, service: str | None = None):
    supervisor = request.app.state.services.runtime_supervisor
    return {"events": await supervisor.history.recent(limit=limit, service=service)}


@router.get("/desktop/apps")
async def desktop_apps(request: Request):
    return request.app.state.services.desktop.list_apps()


@router.get("/desktop/apps/find")
async def desktop_apps_find(request: Request, q: str, limit: int = 8):
    return await asyncio.to_thread(
        request.app.state.services.desktop.find, q, max(1, min(limit, 20))
    )


@router.post("/desktop/apps/open")
async def desktop_apps_open(payload: DesktopOpenRequest, request: Request):
    controller = request.app.state.services.desktop
    return await controller.launch_dynamic(payload.query, origin="api")


@router.post("/desktop/presence/{action}")
async def desktop_presence(action: str, request: Request):
    services = request.app.state.services
    command = {"show": "presence_show", "hide": "presence_hide", "toggle": "presence_toggle"}.get(action)
    if command is None:
        raise HTTPException(status_code=404, detail="Ação de presença inexistente")
    await services.event_bus.publish(
        EventType.UI_COMMAND,
        command=command,
        origin="backend",
        turn_id=None,
    )
    return {"accepted": True, "command": command}


@router.get("/desktop/windows")
async def desktop_windows(request: Request, app: str | None = None):
    return request.app.state.services.desktop.status_windows(app)


@router.post("/desktop/apps/{app_id}/launch")
async def desktop_app_launch(request: Request, app_id: str):
    return await request.app.state.services.desktop.launch(app_id, origin="api")


# ============================== Universal Operator (nyra-full §29/§34) =======

# nyra-7c: observabilidade das camadas de autonomia (sem UI nova).

@router.get("/computer/state")
async def computer_state_route(request: Request):
    services = request.app.state.services
    state = getattr(services, "computer_state", None)
    if state is None:
        raise HTTPException(status_code=503, detail="ComputerState indisponível")
    slots = {}
    for key, slot in state.slots.items():
        if key.startswith(("last_target", "foreground_app", "open_apps",
                           "last_successful_action", "last_foreground_window")):
            slots[key] = {"value": slot.value, "freshness": slot.freshness(state.clock()).value,
                          "source": slot.source}
    return {"slots": slots, "user_activity": state.user_activity(),
            "world_summary": state.world_summary()}


@router.get("/computer/usage/stats")
async def computer_usage_stats(request: Request):
    services = request.app.state.services
    usage = getattr(services, "usage_learning", None)
    if usage is None:
        raise HTTPException(status_code=503, detail="UsageLearning indisponível")
    return {
        "events_recent": usage.recent_events(limit=20),
        "aliases": {k: v.model_dump() for k, v in usage.aliases.items()},
        "preferences": {k: v.model_dump() for k, v in usage.preferences.items()},
        "workflow_candidates": {k: v.model_dump() for k, v in usage.workflows.items()},
    }


@router.get("/computer/skills")
async def computer_skills_list(request: Request, q: str | None = None):
    services = request.app.state.services
    memory = getattr(services, "skill_memory", None)
    if memory is None:
        raise HTTPException(status_code=503, detail="SkillMemory indisponível")
    probe = None
    if q:
        matched = memory.match(q)
        probe = None if matched is None else {"id": matched[0].skill_id,
                                              "name": matched[0].name,
                                              "by": matched[1]}
    return {"skills": memory.list_skills(include_candidates=True),
            "match_probe": probe, "memory_is_none": memory is None}


@router.post("/computer/skills/explicit")
async def computer_skill_explicit(request: Request):
    """Fixture/E2E: 'aprende isso' explícito (nyra-7c §68) sem LLM."""
    services = request.app.state.services
    memory = getattr(services, "skill_memory", None)
    if memory is None:
        raise HTTPException(status_code=503, detail="SkillMemory indisponível")
    try:
        body = json.loads((await request.body()).decode() or "{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="JSON inválido")
    name_hint = str(body.get("name_hint") or "").strip()
    raw_steps = body.get("steps") or []
    aliases = [str(a) for a in body.get("aliases") or []]
    preconditions = [dict(p) for p in body.get("preconditions") or []
                     if isinstance(p, dict)]
    if not name_hint or not isinstance(raw_steps, list) or len(raw_steps) < 1:
        raise HTTPException(status_code=422,
                            detail="name_hint e steps são obrigatórios")
    parsed_steps = []
    for item in raw_steps[:8]:
        if isinstance(item, dict) and item.get("capability"):
            parsed_steps.append((str(item["capability"]), str(item.get("target", ""))))
    if not parsed_steps:
        raise HTTPException(status_code=422, detail="steps inválidos")
    skill = memory.explicit_learn(parsed_steps, name_hint=name_hint, aliases=aliases)
    if preconditions:
        skill.preconditions = preconditions
        memory.persist()
    return skill.model_dump(mode="json")


@router.post("/computer/skills/{skill_id}/promote")
async def computer_skill_promote(skill_id: str, request: Request):
    services = request.app.state.services
    memory = getattr(services, "skill_memory", None)
    if memory is None:
        raise HTTPException(status_code=503, detail="SkillMemory indisponível")
    skill = memory.promote(skill_id, force=True)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill não encontrada")
    return skill.model_dump(mode="json")


@router.get("/apps/registry/status")
async def apps_registry_status(request: Request):
    services = request.app.state.services
    desktop = getattr(services, "desktop", None)
    if desktop is None:
        raise HTTPException(status_code=503, detail="Desktop controller indisponível")
    return desktop.universal_status()


@router.post("/apps/registry/refresh")
async def apps_registry_refresh(request: Request):
    services = request.app.state.services
    desktop = getattr(services, "desktop", None)
    if desktop is None:
        raise HTTPException(status_code=503, detail="Desktop controller indisponível")
    return await asyncio.to_thread(desktop.refresh_universal, True)


@router.get("/apps/registry/diagnostics")
async def apps_registry_diagnostics(request: Request, query: str):
    """Diagnóstico de resolução (somente Developer): candidatos e scores."""
    services = request.app.state.services
    desktop = getattr(services, "desktop", None)
    if desktop is None:
        raise HTTPException(status_code=503, detail="Desktop controller indisponível")
    fast = desktop.universal.resolve_fast(query)
    resolution = desktop.discovery.resolve(query)
    return {
        "query": query,
        "fast_hit": fast.public_dict() if fast else None,
        "discovery": {
            "status": resolution.get("status"),
            "candidate": resolution.get("candidate"),
            "candidates": (resolution.get("candidates") or [])[:5],
        },
        "last_controlled": desktop.last_controlled,
    }


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Event bus broadcast + contrato de voz nyra.voice.v1 (Apêndice PRO E).

    Mensagens aceitas do cliente: hello → hello_ack; voice.barge_in e tts.stop →
    cancelam SOMENTE o TTS atual (sem derrubar Agent Run); heartbeat → ack.
    Texto não-JSON é ignorado por compatibilidade.
    """
    await websocket.accept()
    services = websocket.app.state.services
    event_bus = services.event_bus
    outbound_lock = asyncio.Lock()
    hello_state: dict[str, Any] = {"handshake": False, "satellite_id": None}

    async def _send(payload: dict) -> None:
        async with outbound_lock:
            await websocket.send_json(payload)

    async def forward(event: Event) -> None:
        if not _voice_satellite_event_allowed(event, hello_state.get("satellite_id")):
            return
        await _send(event.model_dump(mode="json"))

    async def receiver() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            message_type = str(message.get("type") or "")
            if message_type == "hello":
                hello_state["handshake"] = True
                hello_state["satellite_id"] = str(message.get("satellite_id") or "") or None
                logger.info("voice_hello", extra={
                    "protocol": str(message.get("protocol_version") or "nyra.voice.v1"),
                    "satellite_id": str(message.get("satellite_id") or "unknown"),
                })
                await _send({
                    "type": "hello_ack",
                    "protocol": "nyra.voice.v1",
                    "accepted": True,
                    "character": "NYRA",
                })
            elif message_type in {"voice.barge_in", "tts.stop"}:
                orchestrator = getattr(services, "orchestrator", None)
                if orchestrator is not None and hasattr(orchestrator, "cancel_speech"):
                    await orchestrator.cancel_speech(reason="satellite_barge_in")
            elif message_type == "heartbeat":
                await _send({"type": "heartbeat_ack"})

    receiver_task = asyncio.create_task(receiver(), name="nyra-ws-receiver")
    await event_bus.subscribe(forward)
    try:
        await _send({"type": "CONNECTED", "payload": {"character": "NYRA"}})
        await receiver_task
    except WebSocketDisconnect:
        pass
    finally:
        await event_bus.unsubscribe(forward)
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass


# ===================================================================== Operator V2
# Endpoints da fase AUTONOMOUS COMPUTER OPERATOR V2 (prompt9 Parte Q §274-§280).
# Nenhum endpoint retorna segredo (§99/§280): credenciais expõem apenas metadados.


def _v2(request: Request):
    service = getattr(request.app.state.services, "operator_v2", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Operator V2 indisponível")
    return service


@router.post("/monitors")
async def monitor_create(payload: MonitorCreateRequest, request: Request) -> dict:
    from app.operator.monitoring import MonitorJobError

    try:
        return await _v2(request).monitor_jobs.create(payload)
    except MonitorJobError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error_code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/monitors")
async def monitor_list(request: Request, include_terminal: bool = True,
                       limit: int = 100) -> dict:
    return await _v2(request).monitor_jobs.list(
        include_terminal=include_terminal, limit=limit,
    )


@router.get("/monitors/{monitor_id}")
async def monitor_status(request: Request, monitor_id: str) -> dict:
    result = await _v2(request).monitor_jobs.status(monitor_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="MonitorJob não encontrado")
    return result


@router.post("/monitors/{monitor_id}/cancel")
async def monitor_cancel(request: Request, monitor_id: str) -> dict:
    result = await _v2(request).monitor_jobs.cancel(monitor_id)
    if not result.get("success"):
        status = 404 if result.get("error_code") == "MONITOR_NOT_FOUND" else 409
        raise HTTPException(status_code=status, detail=result)
    return result


@router.get("/operator/v2/status")
async def operator_v2_status(request: Request) -> dict:
    return _v2(request).status()


@router.get("/watchdog/status")
async def watchdog_status(request: Request) -> dict:
    """Watchdog externo reporta via heartbeat file (backend não é a fonte)."""
    import json as _json
    import time as _time

    settings = request.app.state.services.settings
    path = settings.watchdog_heartbeat_path
    if not path.exists():
        return {"success": True, "running": False,
                "message": "Sem heartbeat do watchdog (não está em execução?)."}
    try:
        document = _json.loads(path.read_text("utf-8"))
        age = round(max(0.0, _time.time() - float(document.get("timestamp", 0))), 1)
        document["heartbeat_age_seconds"] = age
        # Contrato normalizado (closure §16.4/§27): stale/vivo/disabled sempre
        # distinguíveis; heartbeat fresco ⇒ watchdog em execução.
        document["stale"] = age > 30
        document["running"] = not document["stale"]
        document["success"] = True
        return document
    except (OSError, ValueError):
        return {"success": False, "error_code": "HEARTBEAT_UNREADABLE"}


# ------------------------------------------------------------------ vision (Parte A)
@router.post("/vision/capture")
async def vision_capture(request: Request, payload: dict) -> dict:
    import asyncio as _asyncio

    from app.operator.vision_capture import CaptureError

    vision = _v2(request).vision
    if vision is None:
        raise HTTPException(status_code=503, detail="Vision desabilitada por configuração")
    try:
        return await _asyncio.to_thread(
            vision.capture, target=str(payload.get("target") or "window"),
            hwnd=payload.get("hwnd"), monitor_id=int(payload.get("monitor_id") or 1),
            region=payload.get("region"),
        )
    except CaptureError as exc:
        raise HTTPException(status_code=400, detail=exc.code)


@router.get("/vision/frames")
async def vision_frames(request: Request) -> dict:
    vision = _v2(request).vision
    if vision is None:
        raise HTTPException(status_code=503, detail="Vision desabilitada")
    return {"success": True, "frames": vision.frames.list_ids()}


@router.delete("/vision/frames/{frame_id}")
async def vision_drop_frame(request: Request, frame_id: str) -> dict:
    vision = _v2(request).vision
    if vision is None:
        raise HTTPException(status_code=503, detail="Vision desabilitada")
    dropped = vision.frames.drop(frame_id)
    return {"success": dropped, "error_code": None if dropped else "FRAME_NOT_FOUND"}


# ------------------------------------------------------------- adapters (Parte B)
@router.get("/adapters")
async def adapters_list(request: Request) -> dict:
    items = []
    for adapter in _v2(request).adapters.all_adapters():
        items.append({"app_id": adapter.app_id, "display_name": adapter.display_name,
                      "detected": adapter.detect(), "capabilities": adapter.capabilities()})
    return {"success": True, "adapters": items}


@router.post("/adapters/{app_id}/action")
async def adapter_action(request: Request, app_id: str, payload: dict) -> dict:
    registry = _v2(request).adapters
    resolution = await registry.resolve(app_id)
    if not resolution.get("success"):
        raise HTTPException(status_code=404, detail=resolution)
    adapter = registry.by_id(app_id)
    outcome = await adapter.execute_action(str(payload.get("action") or ""),
                                           dict(payload.get("params") or {}))
    return outcome


# ------------------------------------------------------- credentials (Parte D/Q)
@router.get("/credentials")
async def credentials_list(request: Request) -> dict:
    broker = _v2(request).credentials
    if broker is None:
        raise HTTPException(status_code=503, detail="Credential Broker desabilitado")
    return broker.list_credentials()


@router.get("/credentials/{credential_id}/status")
async def credential_status(request: Request, credential_id: str) -> dict:
    broker = _v2(request).credentials
    if broker is None:
        raise HTTPException(status_code=503, detail="Credential Broker desabilitado")
    result = broker.status(credential_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Credencial inexistente")
    return result


@router.put("/credentials/{credential_id}")
async def credential_upsert(request: Request, credential_id: str) -> dict:
    """Criação/rotação SOMENTE via API local pelo operador — o LLM nunca envia o
    segredo (§86/§90). Body: {secret, kind?, description?}."""
    from pydantic import BaseModel as _BaseModel, Field as _Field

    class _CredentialUpsert(_BaseModel):
        secret: str = _Field(min_length=1, max_length=4096)
        kind: str = _Field(default="generic", max_length=80)
        description: str = _Field(default="", max_length=240)
        approval_id: str | None = _Field(default=None, max_length=128)

        model_config = {"extra": "forbid"}

    try:
        payload = _CredentialUpsert.model_validate(await _json_body(request))
    except Exception:
        raise HTTPException(status_code=422, detail="Body requer {secret, kind?, description?}")
    broker = _v2(request).credentials
    if broker is None:
        raise HTTPException(status_code=503, detail="Credential Broker desabilitado")
    existing = broker.status(credential_id)
    approval_id = payload.approval_id or request.headers.get("x-approval-id")
    if existing.get("success"):
        result = broker.rotate(credential_id, payload.secret,
                               approval_id=approval_id)
    else:
        result = broker.create(credential_id, payload.secret, kind=payload.kind,
                               description=payload.description, approval_id=approval_id)
    if not result.get("success") and result.get("error_code") == "APPROVAL_REQUIRED":
        return result  # 200 com approval_required para o fluxo de duas fases do painel
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result)
    return result


async def _json_body(request: Request) -> dict:
    try:
        document = await request.json()
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


@router.delete("/credentials/{credential_id}")
async def credential_delete(request: Request, credential_id: str) -> dict:
    broker = _v2(request).credentials
    if broker is None:
        raise HTTPException(status_code=503, detail="Credential Broker desabilitado")
    result = broker.delete(credential_id,
                           approval_id=request.headers.get("x-approval-id"))
    if not result.get("success") and result.get("error_code") == "APPROVAL_REQUIRED":
        return result
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


# --------------------------------------------------- elevated sessions (Parte E/Q)
@router.post("/elevated/session/open")
async def elevated_session_open(request: Request, payload: dict) -> dict:
    return await _v2(request).elevated.open(
        reason=str(payload.get("reason") or ""),
        ttl_seconds=payload.get("ttl_seconds"),
        approval_id=payload.get("approval_id"),
    )


@router.get("/elevated/session/status")
async def elevated_session_status(request: Request) -> dict:
    return _v2(request).elevated.status()


@router.post("/elevated/session/close")
async def elevated_session_close(request: Request, payload: dict) -> dict:
    return await _v2(request).elevated.close(str(payload.get("session_id") or ""))


# --------------------------------------------------------------- jobs (Parte F/Q)
@router.post("/jobs")
async def job_start(request: Request, payload: dict) -> dict:
    raise HTTPException(
        status_code=403,
        detail="Criação arbitrária de jobs exige autorização emitida por system_shell",
    )


@router.get("/jobs")
async def job_list(request: Request, include_terminal: bool = False) -> dict:
    jobs = _v2(request).jobs
    if jobs is None:
        raise HTTPException(status_code=503, detail="Persistent Jobs desabilitado")
    return await jobs.list(include_terminal=include_terminal)


@router.get("/jobs/{job_id}")
async def job_status(request: Request, job_id: str) -> dict:
    jobs = _v2(request).jobs
    if jobs is None:
        raise HTTPException(status_code=503, detail="Persistent Jobs desabilitado")
    result = await jobs.status(job_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/jobs/{job_id}/logs")
async def job_logs(request: Request, job_id: str, lines: int = 80) -> dict:
    jobs = _v2(request).jobs
    if jobs is None:
        raise HTTPException(status_code=503, detail="Persistent Jobs desabilitado")
    return await jobs.logs(job_id, lines=max(5, min(lines, 300)))


for _action in ("cancel", "pause", "resume"):
    async def _job_action(request: Request, job_id: str, _bound=_action) -> dict:
        jobs = request.app.state.services.operator_v2.jobs
        if jobs is None:
            raise HTTPException(status_code=503, detail="Persistent Jobs desabilitado")
        return await getattr(jobs, _bound)(job_id)

    _job_action.__name__ = f"job_{_action}"
    router.add_api_route(f"/jobs/{{job_id}}/{_action}", _job_action, methods=["POST"])


# -------------------------------------------------------------- tasks (Parte G/Q)
@router.post("/tasks")
async def task_create(request: Request, payload: dict) -> dict:
    from app.operator.tasks import TaskValidationError

    tasks = _v2(request).tasks
    try:
        outcome = await tasks.create_task(
            str(payload.get("goal") or ""),
            [dict(step) for step in (payload.get("steps") or [])],
            verification_plan=str(payload.get("verification_plan") or ""),
            deadline_seconds=payload.get("deadline_seconds"),
        )
    except TaskValidationError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": error.code,
            "message": str(error),
            "stage": "operator_tasks",
            "recoverable": True,
        }) from error
    if outcome.get("success") and payload.get("auto_run", True):
        await tasks.run_task(outcome["task"]["task_id"])
    return outcome


@router.get("/tasks")
async def task_list(request: Request) -> dict:
    return await _v2(request).tasks.list_tasks(include_terminal=False)


@router.get("/tasks/{task_id}")
async def task_status(request: Request, task_id: str) -> dict:
    result = await _v2(request).tasks.status(task_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: str) -> dict:
    return await _v2(request).tasks.cancel(task_id)


# ------------------------------------------------------------ watches (Parte I/Q)
@router.post("/watches")
async def watch_register(request: Request, payload: dict) -> dict:
    watcher = _v2(request).watcher
    if watcher is None:
        raise HTTPException(status_code=503, detail="Desktop Watcher desabilitado")
    try:
        return await watcher.register([str(item) for item in (payload.get("event_types") or [])],
                                      filters=dict(payload.get("filters") or {}),
                                      ttl_seconds=payload.get("ttl_seconds"))
    except Exception as exc:
        code = getattr(exc, "code", "WATCH_INVALID")
        raise HTTPException(status_code=400, detail={"error_code": code, "message": str(exc)})


@router.get("/watches")
async def watch_list(request: Request) -> dict:
    watcher = _v2(request).watcher
    if watcher is None:
        raise HTTPException(status_code=503, detail="Desktop Watcher desabilitado")
    return watcher.status()


@router.get("/watches/{watch_id}/events")
async def watch_events(request: Request, watch_id: str, after_index: int = 0) -> dict:
    watcher = _v2(request).watcher
    if watcher is None:
        raise HTTPException(status_code=503, detail="Desktop Watcher desabilitado")
    result = watcher.events(watch_id, after_index)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.delete("/watches/{watch_id}")
async def watch_cancel(request: Request, watch_id: str) -> dict:
    watcher = _v2(request).watcher
    if watcher is None:
        raise HTTPException(status_code=503, detail="Desktop Watcher desabilitado")
    result = await watcher.cancel(watch_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


# --------------------------------------------------------- workflows (Parte J/Q)
@router.get("/workflows")
async def workflow_list(request: Request) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    return workflows.list_workflows()


@router.post("/workflows")
async def workflow_create(request: Request, payload: dict) -> dict:
    from app.operator.workflows import WorkflowDefinition, WorkflowStep

    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    try:
        steps = [WorkflowStep.model_validate(step) for step in (payload.get("steps") or [])]
        definition = WorkflowDefinition(
            workflow_id=str(payload.get("workflow_id") or ""),
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            trigger_phrases=[str(item) for item in (payload.get("trigger_phrases") or [])],
            steps=steps,
            parameters=dict(payload.get("parameters") or {}),
            risk=str(payload.get("risk") or "LOW_RISK"),
        )
    except Exception as exc:  # pydantic ValidationError
        raise HTTPException(status_code=422, detail=str(exc)[:400])
    result = await workflows.create(definition)
    if not result.get("success") and result.get("error_code") not in {"WORKFLOW_EXISTS"}:
        raise HTTPException(status_code=422, detail=result)
    return result


@router.post("/workflows/{workflow_id}/run")
async def workflow_run(request: Request, workflow_id: str, payload: dict | None = None) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    return await workflows.run(workflow_id, dict(payload or {}))


@router.post("/workflows/{workflow_id}/dry-run")
async def workflow_dry_run(request: Request, workflow_id: str, payload: dict | None = None) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    result = workflows.dry_run(workflow_id, dict(payload or {}))
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.delete("/workflows/{workflow_id}")
async def workflow_delete(request: Request, workflow_id: str) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    result = await workflows.delete(workflow_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/workflows/{workflow_id}/preflight")
async def workflow_preflight(request: Request, workflow_id: str, payload: dict | None = None) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    result = workflows.preflight(workflow_id, dict(payload or {}))
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/workflows/runs")
async def workflow_runs_list(request: Request, limit: int = 25) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    return await workflows.history(limit=int(limit))


@router.get("/workflows/runs/{run_id}")
async def workflow_run_detail(request: Request, run_id: str) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    result = await workflows.history(run_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/workflows/runs/{run_id}/resume")
async def workflow_run_resume(request: Request, run_id: str) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    result = await workflows.resume(run_id)
    if not result.get("success") and result.get("error_code") in {"RUN_NOT_FOUND"}:
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/workflows/runs/{run_id}/cancel")
async def workflow_run_cancel(request: Request, run_id: str) -> dict:
    workflows = _v2(request).workflows
    if workflows is None:
        raise HTTPException(status_code=503, detail="Workflow Engine desabilitada")
    result = await workflows.cancel(run_id)
    if not result.get("success"):
        status_code = 404 if result.get("error_code") == "RUN_NOT_FOUND" else 409
        raise HTTPException(status_code=status_code, detail=result)
    return result


# ------------------------------------------------------ consolidated health report
@router.get("/health_report")
async def health_report(request: Request) -> dict:
    from app.core.health_matrix import build_health_report

    return await build_health_report(request.app.state.services)


# ------------------------------------------------------------ daily check (§240+)
@router.post("/daily_check/run")
async def daily_check_run(request: Request) -> dict:
    from app.core.daily_check import run_daily_check

    return await run_daily_check(request.app.state.services)


@router.get("/daily_check/history")
async def daily_check_history(limit: int = 30) -> dict:
    from app.core.daily_check import load_history

    items = load_history(limit=int(limit))
    regression = _daily_regression(items)
    return {"success": True, "history": items, "count": len(items),
            "regression": regression}


def _daily_regression(items: list[dict]) -> dict | None:
    """Compara as duas execuções mais recentes por categoria (§247)."""
    if len(items) < 2:
        return None
    previous, latest = items[-2], items[-1]
    changes = []
    categories = set(previous.get("categories", {})) | set(latest.get("categories", {}))
    for category in sorted(categories):
        before = (previous.get("categories", {}).get(category) or {}).get("result")
        after = (latest.get("categories", {}).get(category) or {}).get("result")
        if before != after:
            changes.append({"category": category, "before": before, "after": after,
                            "worse": _RESULT_ORDER.get(after, 0) > _RESULT_ORDER.get(before, 0)})
    return {"previous_at": previous.get("generated_at"),
            "latest_at": latest.get("generated_at"),
            "overall_before": previous.get("overall"),
            "overall_latest": latest.get("overall"),
            "changes": changes}


_RESULT_ORDER = {"PASS": 0, "SKIPPED": 0, "DEGRADED": 1, "FAIL": 2}


# ------------------------------------------------------- model benchmark lab (K-Q)
class ModelNotInstalledError(Exception):
    def __init__(self, model_id: str) -> None:
        super().__init__(f"MODEL_NOT_INSTALLED:{model_id}")
        self.model_id = model_id


def _benchmark_lab(request: Request):
    from app.benchmark.lab import ModelNotInstalled as _LabModelNotInstalled

    lab = getattr(request.app.state, "benchmark_lab", None)
    if lab is None:
        raise HTTPException(status_code=503, detail={"error_code": "BENCHMARK_LAB_UNAVAILABLE"})
    return _LabAdapted(lab)


class _LabAdapted:
    """Adapter converting lab's ModelNotInstalled into routes-local error type."""

    def __init__(self, lab) -> None:
        self._lab = lab

    async def require_installed(self, model_id: str) -> None:
        try:
            await self._lab.require_installed(model_id)
        except Exception as error:
            if type(error).__name__ == "ModelNotInstalled":
                raise ModelNotInstalledError(getattr(error, "model_id", "")) from error
            raise

    def __getattr__(self, item):
        return getattr(self._lab, item)


def _benchmark_model_payload(payload: dict) -> tuple[str, list[int], int]:
    model_id = str(payload.get("model_id") or "").strip()
    if not model_id or "/" in model_id or ".." in model_id:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_MODEL_ID"})
    contexts = [int(item) for item in (payload.get("contexts") or [])]
    repeats = int(payload.get("repeats") or 3)
    return model_id, contexts, repeats


@router.get("/benchmark/profiles")
async def benchmark_profiles(request: Request) -> dict:
    return await _benchmark_lab(request).profiles_overview()


@router.post("/benchmark/perf")
async def benchmark_perf(request: Request, payload: dict) -> dict:
    model_id, contexts, repeats = _benchmark_model_payload(payload)
    lab = _benchmark_lab(request)
    try:
        await lab.require_installed(model_id)
    except ModelNotInstalledError as error:
        return {"success": False, "installed": False, "display_state": "NOT INSTALLED",
                "error_code": "MODEL_NOT_INSTALLED", "model_id": error.model_id}
    return lab.start_run("perf", model_id=model_id, contexts=contexts, repeats=repeats)


@router.post("/benchmark/quality")
async def benchmark_quality(request: Request, payload: dict) -> dict:
    model_id, _contexts, _repeats = _benchmark_model_payload(payload)
    lab = _benchmark_lab(request)
    try:
        await lab.require_installed(model_id)
    except ModelNotInstalledError as error:
        return {"success": False, "installed": False, "display_state": "NOT INSTALLED",
                "error_code": "MODEL_NOT_INSTALLED", "model_id": error.model_id}
    return lab.start_run("quality", model_id=model_id)


@router.post("/benchmark/full")
async def benchmark_full(request: Request, payload: dict) -> dict:
    model_id, contexts, repeats = _benchmark_model_payload(payload)
    lab = _benchmark_lab(request)
    try:
        await lab.require_installed(model_id)
    except ModelNotInstalledError as error:
        return {"success": False, "installed": False, "display_state": "NOT INSTALLED",
                "error_code": "MODEL_NOT_INSTALLED", "model_id": error.model_id}
    return lab.start_run("full", model_id=model_id, contexts=contexts, repeats=repeats)


@router.get("/benchmark/runs")
async def benchmark_runs(request: Request) -> dict:
    runs = _benchmark_lab(request).registry.list()
    return {"success": True, "runs": runs, "count": len(runs)}


@router.get("/benchmark/runs/{run_id}")
async def benchmark_run_detail(request: Request, run_id: str) -> dict:
    entry = _benchmark_lab(request).registry.get(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail={"error_code": "RUN_NOT_FOUND"})
    return entry


@router.get("/benchmark/baselines")
async def benchmark_baselines(request: Request) -> dict:
    baselines = _benchmark_lab(request).list_baselines()
    return {"success": True, "baselines": baselines, "count": len(baselines)}


@router.post("/benchmark/baselines/save")
async def benchmark_baseline_save(request: Request, payload: dict) -> dict:
    run_id = str(payload.get("run_id") or "")
    label = str(payload.get("label") or "")
    if not run_id:
        raise HTTPException(status_code=422, detail={"error_code": "RUN_ID_REQUIRED"})
    result = _benchmark_lab(request).save_baseline(run_id, label)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/benchmark/compare")
async def benchmark_compare(request: Request, payload: dict) -> dict:
    baseline = str(payload.get("baseline") or "")
    candidate = str(payload.get("candidate") or "")
    if not baseline or not candidate:
        raise HTTPException(status_code=422, detail={"error_code": "LABELS_REQUIRED"})
    return _benchmark_lab(request).compare(baseline, candidate)


# =====================================================================
# OPERATIONS UI V3 (prompt11)
# Feature Control Center · Settings Service V3 · Integration Center ·
# HA Profiles · Voice Bridge · Release Health · World State · About
# Todos os endpoints usam envelope de erro seguro (core.errors).
# =====================================================================


def _v3_services(request: Request):
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail={
            "error_code": "BACKEND_NOT_READY",
            "message": "Serviços ainda inicializando. Aguarde alguns segundos.",
            "stage": "backend", "recoverable": True,
        })
    return services


def _v3_bridge(services) -> "VoiceProcessorBridge":
    bridge = getattr(services, "voice_bridge", None)
    if bridge is None:
        from app.speech.external_bridge import VoiceProcessorBridge

        bridge = VoiceProcessorBridge(services.settings)
        services.voice_bridge = bridge
    return bridge


# ---------------------------------------------------------------- capabilities
@router.get("/capabilities")
async def capabilities_list(request: Request) -> dict:
    return await get_capabilities(_v3_services(request))


class CapabilityToggleRequest(BaseModel):
    enabled: bool


@router.put("/capabilities/{capability_id}")
async def capability_toggle(request: Request, capability_id: str,
                            payload: CapabilityToggleRequest) -> dict:
    try:
        return await set_capability(
            _v3_services(request), capability_id, payload.enabled
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "CAPABILITY_UNKNOWN",
            "message": f"Capability '{capability_id}' não registrada.",
            "stage": "capabilities", "recoverable": True,
        }) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail={
            "error_code": "CAPABILITY_NOT_TOGGLEABLE",
            "message": str(error),
            "stage": "capabilities", "recoverable": False,
        }) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "CAPABILITY_INVALID_PAYLOAD",
            "message": str(error), "stage": "capabilities", "recoverable": True,
        }) from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail={
            "error_code": "CAPABILITY_APPLY_FAILED",
            "message": f"{error} Estado revertido; verifique o health do subsistema.",
            "stage": "capabilities", "recoverable": True,
        }) from error


# ---------------------------------------------------------------- settings v3
@router.get("/settings/v3")
async def settings_v3(request: Request) -> dict:
    return get_settings_v3(_v3_services(request).settings)


class SettingUpdateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    value: Any


@router.put("/settings/v3")
async def setting_update_v3(request: Request, payload: SettingUpdateRequest) -> dict:
    try:
        services = _v3_services(request)
        result = update_setting(services.settings, payload.key, payload.value)
        if payload.key.startswith("selfdev_") and getattr(services, "selfdev", None) is not None:
            await services.selfdev.refresh_settings()
        return result
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "SETTING_UNKNOWN",
            "message": f"Setting '{payload.key}' não exposta na UI.",
            "stage": "settings", "recoverable": True,
        }) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail={
            "error_code": "SETTING_IS_SECRET",
            "message": str(error), "stage": "settings", "recoverable": True,
        }) from error
    except SettingValidationError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": error.error_code,
            "message": str(error), "stage": "settings", "recoverable": True,
        }) from error


@router.get("/config/export")
async def config_export(request: Request) -> dict:
    """Export seguro de configurações NÃO secretas (§235-§236)."""
    services = _v3_services(request)
    about = about_payload(services)
    return export_config(services.settings, about)


# ------------------------------------------------------- self-development v1
def _selfdev(request: Request):
    service = getattr(_v3_services(request), "selfdev", None)
    if service is None:
        raise HTTPException(status_code=503, detail={
            "error_code": "SELFDEV_NOT_READY",
            "message": "Self-Development Service ainda não está disponível.",
            "stage": "selfdev", "recoverable": True,
        })
    return service


class SelfDevIssueRequest(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=3, max_length=2000)
    components: list[str] = Field(default_factory=list, max_length=30)


class SelfDevRunRequest(BaseModel):
    issue_id: str | None = Field(default=None, pattern=r"^SELFDEV-[A-Z0-9-]{4,64}$")


class SelfDevRevertRequest(BaseModel):
    approval_id: str | None = Field(default=None, max_length=200)


@router.get("/selfdev/status")
async def selfdev_status(request: Request) -> dict:
    return _selfdev(request).status()


@router.get("/selfdev/issues")
async def selfdev_issues(request: Request) -> dict:
    values = _selfdev(request).issues()
    return {"issues": values, "count": len(values)}


@router.post("/selfdev/issues")
async def selfdev_issue_submit(request: Request, payload: SelfDevIssueRequest) -> dict:
    return _selfdev(request).submit_explicit_issue(payload.title, payload.description, payload.components)


@router.get("/selfdev/issues/{issue_id}")
async def selfdev_issue_details(request: Request, issue_id: str) -> dict:
    value = _selfdev(request).issue_details(issue_id)
    if value is None:
        raise HTTPException(status_code=404, detail={"error_code": "SELFDEV_ISSUE_NOT_FOUND"})
    return value


@router.get("/selfdev/issues/{issue_id}/diff")
async def selfdev_issue_diff(request: Request, issue_id: str) -> dict:
    value = await _selfdev(request).issue_diff(issue_id)
    if value is None:
        raise HTTPException(status_code=404, detail={"error_code": "SELFDEV_ISSUE_NOT_FOUND"})
    return value


@router.post("/selfdev/issues/{issue_id}/revert")
async def selfdev_issue_revert(request: Request, issue_id: str, payload: SelfDevRevertRequest) -> dict:
    return await _selfdev(request).revert(issue_id, approval_id=payload.approval_id)


@router.post("/selfdev/run-once")
async def selfdev_run_once(request: Request, payload: SelfDevRunRequest) -> dict:
    return await _selfdev(request).run_once(issue_id=payload.issue_id, bypass_idle=True)


@router.get("/selfdev/repository/query")
async def selfdev_repository_query(request: Request, q: str) -> dict:
    question = q.strip()
    if not 2 <= len(question) <= 500:
        raise HTTPException(status_code=422, detail={"error_code": "SELFDEV_QUERY_INVALID"})
    return _selfdev(request).repository_query(question)


@router.get("/selfdev/models")
async def selfdev_models(request: Request) -> dict:
    try:
        values = await _selfdev(request).installed_models()
    except (PermissionError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail={
            "error_code": str(error)[:120],
            "message": "O SelfDev aceita somente modelos Ollama locais já instalados.",
            "stage": "selfdev", "recoverable": True,
        }) from error
    return {"models": values, "selected": _selfdev(request).settings.model}


@router.get("/selfdev/notifications")
async def selfdev_notifications(request: Request, unread_only: bool = False, limit: int = 100) -> dict:
    service = _selfdev(request)
    values = service.notification_items(unread_only=unread_only, limit=limit)
    return {"notifications": values, "unread": service.notifications.unread_count()}


@router.post("/selfdev/notifications/{notification_id}/read")
async def selfdev_notification_read(request: Request, notification_id: str) -> dict:
    if not _selfdev(request).mark_notification_read(notification_id):
        raise HTTPException(status_code=404, detail={"error_code": "SELFDEV_NOTIFICATION_NOT_FOUND"})
    return {"success": True, "notification_id": notification_id}


# ----------------------------------------------------------- integration center
@router.get("/integrations/status")
async def integrations_status_route(request: Request) -> dict:
    return await integrations_status(_v3_services(request))


@router.post("/integrations/{integration_id}/{action}")
async def integrations_action(request: Request, integration_id: str, action: str) -> dict:
    if action not in {"test", "enable", "disable", "reconnect", "diagnostics"}:
        raise HTTPException(status_code=404, detail={
            "error_code": "ACTION_UNKNOWN",
            "message": "Ações válidas: test|enable|disable|reconnect|diagnostics.",
            "stage": "integrations", "recoverable": True,
        })
    try:
        result = await integration_action(
            _v3_services(request), integration_id, action
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "INTEGRATION_UNKNOWN",
            "message": f"Integração '{integration_id}' desconhecida.",
            "stage": "integrations", "recoverable": True,
        }) from error
    except IntegrationActionError as error:
        raise HTTPException(status_code=error.status_code, detail={
            "error_code": error.error_code,
            "message": error.message, "stage": "integrations", "recoverable": True,
        }) from error
    except Exception as error:  # noqa: BLE001 — integração offline não derruba backend
        logger.warning("integration_action_failed id=%s action=%s error=%s",
                       integration_id, action, type(error).__name__)
        raise HTTPException(status_code=502, detail={
            "error_code": "INTEGRATION_ACTION_FAILED",
            "message": f"Ação '{action}' falhou em '{integration_id}' "
                       f"({type(error).__name__}). A integração pode estar offline.",
            "stage": "integrations", "recoverable": True,
        }) from error
    return {"success": True, "result": result}


# ------------------------------------------------------------------ ha profiles
def _require_api_approval(request: Request, *, command: str, resource: str,
                          risk: str = "DESTRUCTIVE", approval_id: str | None = None) -> dict | None:
    """One-use approval for explicit local API mutations."""
    from app.tools.shell_models import ShellRiskLevel

    gate = request.app.state.services.shell.approvals
    fingerprint = gate.fingerprint(command, "local_api", "", 30, target=resource)
    if approval_id:
        granted, reason = gate.consume(approval_id, fingerprint)
        if granted:
            return None
        return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
    record = gate.request(
        command=command, shell="local_api", working_directory="",
        timeout_seconds=30, risk_level=ShellRiskLevel(risk), target=resource,
        fingerprint=fingerprint,
    )
    return {"success": False, "error_code": "APPROVAL_REQUIRED",
            "approval_required": True, "approval_id": record.approval_id}


@router.get("/home-assistant/profiles")
async def ha_profiles() -> dict:
    return list_profiles()


class HAProfileUpsertRequest(BaseModel):
    profile_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    url: str = ""
    enabled: bool = False
    tls: bool = False
    priority: int = Field(default=99, ge=1, le=999)


@router.put("/home-assistant/profiles")
async def ha_profile_upsert(request: Request, payload: HAProfileUpsertRequest) -> dict:
    try:
        record = upsert_profile(payload.model_dump(), _v3_services(request).settings)
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "HA_PROFILE_INVALID",
            "message": str(error), "stage": "homelab", "recoverable": True,
        }) from error
    return {"success": True, "profile": record}


@router.delete("/home-assistant/profiles/{profile_id}")
async def ha_profile_remove(request: Request, profile_id: str,
                            approval_id: str | None = None) -> dict:
    pending = _require_api_approval(
        request, command=f"remove_home_assistant_profile {profile_id}",
        resource=f"credential:home_assistant:{profile_id}", approval_id=approval_id,
    )
    if pending is not None:
        return pending
    try:
        return remove_profile(profile_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "HA_PROFILE_UNKNOWN", "message": "Profile não encontrado.",
            "stage": "homelab", "recoverable": True,
        }) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail={
            "error_code": "HA_PROFILE_PROTECTED", "message": str(error),
            "stage": "homelab", "recoverable": False,
        }) from error


@router.post("/home-assistant/profiles/{profile_id}/activate")
async def ha_profile_activate(request: Request, profile_id: str) -> dict:
    try:
        return await activate_profile(_v3_services(request), profile_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "HA_PROFILE_UNKNOWN", "message": "Profile não encontrado.",
            "stage": "homelab", "recoverable": True,
        }) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail={
            "error_code": "HA_PROFILE_DISABLED", "message": str(error),
            "stage": "homelab", "recoverable": True,
        }) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "HA_PROFILE_NO_URL", "message": str(error),
            "stage": "homelab", "recoverable": True,
        }) from error


class HATokenRequest(BaseModel):
    token: str = ""
    approval_id: str | None = Field(default=None, max_length=128)


@router.post("/home-assistant/profiles/{profile_id}/token")
async def ha_profile_token(request: Request, profile_id: str,
                           payload: HATokenRequest) -> dict:
    if not payload.token.strip():
        pending = _require_api_approval(
            request, command=f"clear_home_assistant_token {profile_id}",
            resource=f"credential:home_assistant:{profile_id}",
            approval_id=payload.approval_id,
        )
        if pending is not None:
            return pending
    try:
        return set_profile_token(_v3_services(request), profile_id, payload.token)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "HA_PROFILE_UNKNOWN", "message": "Profile não encontrado.",
            "stage": "homelab", "recoverable": True,
        }) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "HA_TOKEN_INVALID", "message": str(error),
            "stage": "homelab", "recoverable": True,
        }) from error


@router.post("/home-assistant/profiles/{profile_id}/test")
async def ha_profile_test(request: Request, profile_id: str) -> dict:
    try:
        return await test_profile(_v3_services(request), profile_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "error_code": "HA_PROFILE_UNKNOWN", "message": "Profile não encontrado.",
            "stage": "homelab", "recoverable": True,
        }) from error
    except PermissionError as error:
        # §191: hardware inexistente NUNCA é contatado.
        raise HTTPException(status_code=409, detail={
            "error_code": "HA_PROFILE_DISABLED", "message": str(error),
            "stage": "homelab", "recoverable": True,
        }) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "HA_PROFILE_NO_URL", "message": str(error),
            "stage": "homelab", "recoverable": True,
        }) from error


# ------------------------------------------------------- home assistant entities
# Entity browser (prompt11_1 §23-§26): leitura real via client existente.
@router.get("/home-assistant/entities")
async def ha_entities(request: Request, domain: str = "", search: str = "",
                      state: str = "", limit: int = 50) -> dict:
    services = _v3_services(request)
    if not services.settings.home_assistant_enabled:
        raise HTTPException(status_code=409, detail={
            "error_code": "HA_DISABLED", "message": "Integração Home Assistant desabilitada.",
            "stage": "homelab", "recoverable": True,
        })
    try:
        entities = await services.homelab.ha_list_entities(
            domain=domain, state=state, search=search,
            limit=max(1, min(int(limit), 100)),
        )
    except IntegrationError as error:
        raise HTTPException(status_code=_integration_error_status(error.code), detail={
            "error_code": error.code, "message": error.message,
            "stage": "homelab", "recoverable": True,
        }) from error
    domains = sorted({e["domain"] for e in entities if e.get("domain")})
    return {"entities": entities, "count": len(entities), "domains_present": domains}


@router.get("/home-assistant/entities/{entity_id:path}")
async def ha_entity_detail(request: Request, entity_id: str) -> dict:
    try:
        detail = await _v3_services(request).homelab.ha_get_state(entity_id)
    except IntegrationError as error:
        raise HTTPException(status_code=_integration_error_status(error.code), detail={
            "error_code": error.code, "message": error.message,
            "stage": "homelab", "recoverable": True,
        }) from error
    attributes = detail.get("attributes") or {}
    supported = []
    domain = entity_id.split(".", 1)[0]
    if domain in {"light", "switch", "input_boolean", "media_player", "fan"}:
        supported = ["turn_on", "turn_off", "toggle"]
    elif domain in {"automation", "script", "scene"}:
        supported = ["trigger"] if domain != "scene" else ["run_scene"]
    detail["supported_services"] = supported
    detail["safe_attributes"] = attributes
    return detail


class HAEntityServiceRequest(BaseModel):
    service: str = Field(pattern="^(turn_on|turn_off|toggle|trigger|run_scene)$")
    expected_state: str = Field(default="", max_length=40)
    approval_id: str | None = Field(default=None, max_length=128)


@router.post("/home-assistant/entities/{entity_id:path}/service")
async def ha_entity_service(request: Request, entity_id: str,
                            payload: HAEntityServiceRequest) -> dict:
    """Ação de entidade com ACT→VERIFY (§26): 200 não prova efeito."""
    services = _v3_services(request)
    domain = entity_id.split(".", 1)[0]
    if not re.fullmatch(r"[a-z0-9_]{1,64}", domain):
        raise HTTPException(status_code=422, detail={
            "error_code": "HA_SERVICE_FAILED", "message": "entity_id inválido.",
            "stage": "homelab", "recoverable": True,
        })
    service_data = {"_expected_state": payload.expected_state} if payload.expected_state else None
    result = await services.homelab.ha_call_service(
        domain, payload.service, target={"entity_id": entity_id},
        service_data=service_data,
        approval_id=payload.approval_id,
    )
    status_code = 200 if result.get("success") else (
        202 if result.get("approval_required") else 502)
    return JSONResponse(status_code=status_code, content=result)


# ----------------------------------------------------------------- proxmox UI
# Configuração completa pela interface (prompt11_1 §29-§34).
@router.get("/proxmox/config")
async def proxmox_config_get(request: Request) -> dict:
    return proxmox_ui_config.public_status(_v3_services(request))


class ProxmoxConfigUpdate(BaseModel):
    enabled: bool | None = None
    url: str = Field(default="", max_length=200)
    verify_ssl: bool | None = None
    preferred_node: str = Field(default="", max_length=64)
    timeout_seconds: float | None = Field(default=None, ge=4, le=60)
    token_id: str | None = Field(default=None, max_length=200)
    token_secret: str | None = Field(default=None, max_length=400)


@router.put("/proxmox/config")
async def proxmox_config_put(request: Request, payload: ProxmoxConfigUpdate) -> dict:
    services = _v3_services(request)
    try:
        token_id = (payload.token_id or "").strip()
        token_secret = (payload.token_secret or "").strip()
        if bool(token_id) != bool(token_secret):
            # Validar antes de qualquer persistência mantém a atualização atômica.
            raise HTTPException(status_code=422, detail={
                "error_code": "PROXMOX_TOKEN_PAIR_REQUIRED",
                "message": "Informe Token ID e Token Secret juntos.",
                "stage": "integrations", "recoverable": True,
            })
        # exclude_unset: campos ausentes NÃO sobrescrevem o que já está salvo
        # (prompt11_2 §11 — toggle de enabled não pode apagar a URL).
        updates = payload.model_dump(exclude_none=True, exclude_unset=True)
        previous = proxmox_ui_config.load_config(services.settings)
        config = proxmox_ui_config.save_config(updates)
        endpoint_changed = bool(
            "url" in updates
            and proxmox_ui_config.endpoint_identity(previous.get("url", ""))
            != proxmox_ui_config.endpoint_identity(config.get("url", ""))
        )
        credentials = {}
        if token_id and token_secret:
            credentials = proxmox_ui_config.save_credentials(token_id, token_secret)
        elif endpoint_changed:
            # O par existente foi vinculado à origem anterior; não o envie ao
            # novo endpoint até o operador fornecer um par novo explicitamente.
            credentials = {
                "credentials_reset": True,
                **proxmox_ui_config.disconnect_credentials(),
            }
        config = proxmox_ui_config.load_config(services.settings)
        applied = proxmox_ui_config.apply_to_runtime(services)
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "PROXMOX_CONFIG_INVALID", "message": str(error),
            "stage": "integrations", "recoverable": True,
        }) from error
    return {"success": True, "config": {key: value for key, value in config.items()
                                        if key != "last_test"},
            "endpoint_changed": endpoint_changed,
            "credentials": credentials, "runtime_applied": applied}


@router.post("/proxmox/disconnect")
async def proxmox_disconnect(request: Request,
                             payload: RuntimeActionRequest | None = None) -> dict:
    services = _v3_services(request)
    pending = _require_api_approval(
        request, command="disconnect_proxmox_credentials",
        resource="credential:proxmox", approval_id=(payload.approval_id if payload else None),
    )
    if pending is not None:
        return pending
    removed = proxmox_ui_config.disconnect_credentials()
    applied = proxmox_ui_config.apply_to_runtime(services)
    return {"success": True, **removed, "runtime_applied": applied,
            "status": proxmox_ui_config.public_status(services)}


@router.post("/proxmox/test")
async def proxmox_test_connection(request: Request) -> dict:
    return await proxmox_ui_config.test_connection(_v3_services(request))


@router.get("/proxmox/inventory")
async def proxmox_inventory(request: Request) -> dict:
    services = _v3_services(request)
    status = proxmox_ui_config.public_status(services)
    if not status["enabled"]:
        raise HTTPException(status_code=409, detail={
            "error_code": "PROXMOX_DISABLED", "message": "Integração Proxmox desabilitada.",
            "stage": "homelab", "recoverable": True,
        })
    if not status["auth_configured"]:
        raise HTTPException(status_code=409, detail={
            "error_code": "PROXMOX_UNCONFIGURED",
            "message": "API Token ausente — configure a integração antes de listar o inventário.",
            "stage": "homelab", "recoverable": True,
        })
    try:
        data = await proxmox_ui_config.inventory(services)
    except IntegrationError as error:
        raise HTTPException(status_code=_integration_error_status(error.code), detail={
            "error_code": error.code, "message": error.message,
            "stage": "homelab", "recoverable": True,
        }) from error
    return {**data, "status": status}


# ---------------------------------------------------------------- openwrt UI
# Configuração OpenWrt pela interface (hotfix openwrt_config_hotfix.md).
@router.get("/openwrt/config")
async def openwrt_config_get(request: Request) -> dict:
    return openwrt_ui_config.public_status(_v3_services(request))


class OpenWrtConfigUpdate(BaseModel):
    url: str = Field(default="", max_length=200)
    username: str = Field(default="", max_length=120)
    password: str | None = Field(default=None, max_length=400)


@router.put("/openwrt/config")
async def openwrt_config_put(request: Request, payload: OpenWrtConfigUpdate) -> dict:
    services = _v3_services(request)
    try:
        # exclude_unset: campos ausentes NÃO sobrescrevem o que já está salvo.
        config = openwrt_ui_config.save_config(
            payload.model_dump(exclude_none=True, exclude_unset=True))
        credentials = {}
        password = (payload.password or "").strip()
        if password:
            credentials = openwrt_ui_config.save_credentials(password)
        applied = openwrt_ui_config.apply_to_runtime(services)
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "OPENWRT_CONFIG_INVALID", "message": str(error),
            "stage": "integrations", "recoverable": True,
        }) from error
    return {"success": True, "config": {key: value for key, value in config.items()
                                        if key != "last_test"},
            "credentials": credentials, "runtime_applied": applied}


@router.post("/openwrt/test")
async def openwrt_test_connection(request: Request) -> dict:
    return await openwrt_ui_config.test_connection(_v3_services(request))


class ProxmoxGuestActionRequest(BaseModel):
    action: str = Field(pattern="^(start|shutdown|reboot|stop)$")
    approval_id: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="UI power action", max_length=200)


@router.post("/homelab/proxmox/guests/{reference}/action")
async def proxmox_guest_action(request: Request, reference: str,
                               payload: ProxmoxGuestActionRequest) -> dict:
    """Power ops via executor EXISTENTE do Homelab Control Plane (§37/§38).

    Risco/approval/verificação ficam no controller; Start é LOW_RISK auto e
    Shutdown/Reboot/Stop exigem approval single-use do operador. A resposta só
    é VERIFIED após poll do guest state real — nunca pelo aceite da task.
    """
    services = _v3_services(request)
    result = await services.homelab.proxmox_vm_action(
        f"vm_{payload.action}", reference,
        approval_id=payload.approval_id, reason=payload.reason,
    )
    status_code = 200 if result.get("success") else (
        202 if result.get("approval_required") else 502)
    return JSONResponse(status_code=status_code, content=result)


def _integration_error_status(code: str) -> int:
    if code.endswith("AUTH_MISSING"):
        return 409
    if code.endswith(("AUTH_FAILED", "PERMISSION_DENIED")):
        return 401
    if code.endswith("_DISABLED"):
        return 409
    if "TIMEOUT" in code or "OFFLINE" in code or "UNAVAILABLE" in code:
        return 504
    return 502


# ------------------------------------------------------------------ voice v3
@router.get("/voice/catalog")
async def voice_catalog(request: Request) -> dict:
    """Catálogo único de vozes — fonte única no backend (§132-§134)."""
    from app.speech.tts import tts_provider_catalog

    services = _v3_services(request)
    voices = tts_provider_catalog()
    stt_available = True
    stt_name = getattr(services.stt, "name", "")
    catalog = {
        "stt": {
            "active_provider": stt_name,
            "available": bool(stt_available and stt_name and stt_name != "disabled"),
            "providers": [{"id": "faster-whisper", "available": True,
                           "engine": "faster-whisper + silero-vad"}],
        },
        "tts": {"active_provider": getattr(services.tts, "name", ""),
                "providers": voices},
        "version": 3,
    }
    return catalog


VOICE_PROFILE_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "profile_id": "realtime",
        "name": "Realtime",
        "description": "Streaming completo com barge-in agressivo.",
        "apply": {"voice_stream_tts": True, "voice_barge_in": True,
                  "hands_free_timeout_seconds": 300},
    },
    {
        "profile_id": "natural",
        "name": "Natural",
        "description": "Fala pausada, interrupções moderadas.",
        "apply": {"voice_stream_tts": True, "voice_barge_in": True,
                  "audio_volume": 0.9},
    },
    {
        "profile_id": "low_latency",
        "name": "Low Latency",
        "description": "Prioriza tempo até o primeiro áudio.",
        "apply": {"voice_stream_tts": True, "voice_barge_in": False},
    },
    {
        "profile_id": "external_processor",
        "name": "External Processor",
        "description": "Delega processamento ao bridge externo quando saudável.",
        "apply": {},
        "requires_bridge": True,
    },
)


def get_voice_profiles(settings) -> dict:
    active = str(getattr(settings, "voice_profile_active", "") or "")
    profiles = []
    for preset in VOICE_PROFILE_PRESETS:
        profiles.append({
            **preset,
            "active": preset["profile_id"] == active,
        })
    return {"profiles": profiles, "active": active or None, "version": 1}


@router.get("/voice/profiles")
async def voice_profiles(request: Request) -> dict:
    return get_voice_profiles(_v3_services(request).settings)


@router.post("/voice/profiles/{profile_id}/activate")
async def voice_profile_activate(request: Request, profile_id: str) -> dict:
    services = _v3_services(request)
    settings = services.settings
    preset = next((p for p in VOICE_PROFILE_PRESETS
                   if p["profile_id"] == profile_id), None)
    if preset is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "VOICE_PROFILE_UNKNOWN",
            "message": "Perfis válidos: realtime | natural | low_latency | external_processor.",
            "stage": "voice", "recoverable": True,
        })

    applied: dict[str, Any] = {}
    if preset.get("requires_bridge"):
        bridge = _v3_bridge(services)
        await bridge.set_enabled(True)
        snapshot = bridge.cached_status()
        applied["external_processor"] = {
            "enabled": snapshot["enabled"],
            "health": snapshot["health"],
            "fallback_internal_active": snapshot["fallback_internal_active"],
        }
        save_runtime_settings({"voice_profile_active": profile_id})
        setattr(settings, "voice_profile_active", profile_id)
    else:
        updates = {k: v for k, v in preset["apply"].items()}
        for key, value in updates.items():
            setattr(settings, key, value)
        save_runtime_settings({"voice_profile_active": profile_id, **updates})
        setattr(settings, "voice_profile_active", profile_id)

    return {"success": True, "activated": profile_id, "applied": applied,
            "profiles": get_voice_profiles(settings)["profiles"]}


# ------------------------------------------------------- external voice bridge
@router.get("/voice-bridge/status")
async def voice_bridge_status(request: Request) -> dict:
    services = _v3_services(request)
    return _v3_bridge(services).cached_status()


class VoiceBridgeUpdateRequest(BaseModel):
    enabled: bool | None = None
    endpoint: str | None = Field(default=None, max_length=300)
    protocol: str | None = Field(default=None, pattern="^(http|websocket)$")
    autostart: bool | None = None


@router.put("/voice-bridge/settings")
async def voice_bridge_settings(request: Request,
                                payload: VoiceBridgeUpdateRequest) -> dict:
    services = _v3_services(request)
    bridge = _v3_bridge(services)
    try:
        return await bridge.update(payload.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(status_code=422, detail={
            "error_code": "BRIDGE_INVALID_ENDPOINT",
            "message": str(error), "stage": "voice", "recoverable": True,
        }) from error


@router.post("/voice-bridge/test")
async def voice_bridge_test(request: Request) -> dict:
    services = _v3_services(request)
    bridge = _v3_bridge(services)
    if not bridge.cached_status()["configured"]:
        raise HTTPException(status_code=409, detail={
            "error_code": "BRIDGE_NOT_CONFIGURED",
            "message": "Endpoint do processor externo inválido ou ausente.",
            "stage": "voice", "recoverable": True,
        })
    return await bridge.test()


# --------------------------------------------------- about / release / support
@router.get("/about")
async def about_endpoint(request: Request) -> dict:
    return about_payload(_v3_services(request))


@router.get("/release/health")
async def release_health_endpoint(request: Request) -> dict:
    return release_health(_v3_services(request))


@router.post("/release/revalidate")
async def release_revalidate_endpoint() -> dict:
    """Closure §21: dispara o release gate em background job controlado."""
    from app.core.release_info import start_release_revalidation
    return await start_release_revalidation()


class PowerActionRequest(BaseModel):
    reason: str = Field(default="operator_settings", max_length=200)
    approval_id: str | None = Field(default=None, max_length=128)


@router.post("/runtime/power/{action}")
async def runtime_power_endpoint(request: Request, action: str,
                                 payload: PowerActionRequest | None = None) -> dict:
    """Closure §12/§13: Encerrar NYRA completamente / Reiniciar NYRA completamente.

    Ação iniciada pelo operador na UI (confirmação de uso único no clique) —
    nunca acionável por texto do LLM. Watchdog é desarmado antes da saída.
    """
    from fastapi import HTTPException

    from app.core import lifecycle

    reason = (payload.reason if payload else "") or "operator_settings"
    if action in {"shutdown", "restart"}:
        pending = _require_api_approval(
            request, command=f"runtime_power {action}", resource=f"power:{action}",
            risk="CRITICAL", approval_id=(payload.approval_id if payload else None),
        )
        if pending is not None:
            return pending
    if action == "shutdown":
        return await lifecycle.coordinate_full_shutdown(reason)
    if action == "restart":
        return await lifecycle.coordinate_full_restart(reason)
    raise HTTPException(status_code=422, detail={
        "error_code": "INVALID_POWER_ACTION",
        "message": "Ação deve ser 'shutdown' ou 'restart'.",
        "stage": "lifecycle", "recoverable": True,
    })


@router.get("/runtime/lifecycle")
async def runtime_lifecycle_endpoint() -> dict:
    """Estado do ciclo de vida para a UI (shutdown em curso)."""
    from app.core import lifecycle
    return {"shutting_down": lifecycle.is_shutting_down()}


@router.get("/support/bundle")
async def support_bundle_endpoint(request: Request) -> dict:
    return await support_bundle(_v3_services(request))


@router.get("/world-state")
async def world_state_endpoint(request: Request) -> dict:
    return await world_state_snapshot(_v3_services(request))
