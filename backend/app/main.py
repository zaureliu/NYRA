from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.character import StateMachine
from app.core.config import get_settings
from app.core.errors import unhandled_exception_handler
from app.core.release_info import APP_VERSION
from app.core.logging import configure_logging
from app.core.local_transport import LocalRequestSecurityMiddleware
from app.core.paths import ensure_runtime_directories
from app.core.paths import DATA_ROOT
from app.core.turn import TurnRegistry
from app.conversation import ConversationEngine
from app.events import EventBus
from app.homelab import HomelabControlPlane, HomelabMonitor
from app.homelab.tools import register_homelab_tools
from app.integrations.proxmox import ProxmoxReadOnlyClient
from app.llm import create_llm_provider
from app.llm.warm_manager import OllamaWarmManager
from app.memory import MemoryRepository
from app.listening import AlwaysListeningManager
from app.network_watch import NetworkWatchMonitor
from app.network_watch.alerts import ProactiveNetworkAlerts
from app.integrations.sentinel import ProactiveSentinelAlerts, SentinelConnector
from app.integrations.sentinel.tools import register_sentinel_tools
from app.realtime.models import DuplexMode
from app.realtime.orchestrator import RealtimeOrchestrator
from app.realtime.settings import V4SettingsManager
from app.realtime.telemetry import RealtimeTelemetry
from app.realtime.cooldowns import CooldownManager
from app.perception import PCAwareness
from app.attention import AttentionEngine
from app.reactions import ReactionEngine
from app.proactive import ProactiveEngine
from app.avatar import AvatarController, VTubeStudioAvatarProvider
from app.skills import create_skill_registry
from app.speech.voice_processor import VoiceProcessor
from app.services import Services
from app.speech import FasterWhisperSTT, create_tts_provider
from app.speech.tts import tts_provider_catalog
from app.speech.provider_credentials import TtsCredentialBroker
from app.speech.provider_registry import TtsProviderRegistry, build_tts_provider_registry
from app.speech.vad import VADConfig
from app.speech.recognition.credentials import STTCredentialBroker
from app.speech.recognition.registry import STTProviderRegistry
from app.speech.recognition.api import router as stt_router
from app.speech.queue import SpeechQueue
from app.tools import RemoteShellService, SystemShellService, create_tool_registry, register_network_watch_tools
from app.agent import AgentController
from app.runtime import (
    ProcessManager,
    RuntimeHistory,
    RuntimeSupervisor,
    register_runtime_tools,
)
from app.desktop import DesktopController, register_desktop_tools
from app.voice_hunter import VoiceHunterService
from app.brain import BrainManager
from app.intelligence.api import router as intelligence_router
from app.proactive_presence.api import router as proactive_presence_router
from app.persona_runtime.api import router as persona_runtime_router
from app.emotional_presence.api import router as emotional_presence_router


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("nyra")


async def _prepare_stt(provider: FasterWhisperSTT) -> None:
    try:
        await provider.preload()
        logger.info("stt_prepared", extra={"provider": provider.name, "model": provider.model_name})
    except Exception as error:
        logger.warning("stt_preload_deferred", extra={"error": type(error).__name__})


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_runtime_directories()
    from app.core.lifecycle import consume_intentional_flag, parent_disappeared_shutdown
    from app.core.process_ownership import ParentProcessWatch, owned_parent_pid

    parent_watch = None
    parent_pid = owned_parent_pid()
    if parent_pid is not None:
        try:
            parent_watch = ParentProcessWatch(parent_pid, parent_disappeared_shutdown)
            parent_watch.start()
            logger.info("backend_process_owner parent_pid=%s", parent_pid)
        except (OSError, ValueError):
            logger.error("backend_parent_watch_start_failed parent_pid=%s", parent_pid)

    prior_shutdown = consume_intentional_flag()
    if prior_shutdown:
        logger.info(
            "startup_after_intentional_%s",
            str(prior_shutdown.get("kind") or "shutdown"),
            extra={"reason": str(prior_shutdown.get("reason") or "")[:80]},
        )
    event_bus = EventBus()
    from app.world_state import WorldStateEngine

    world_state = WorldStateEngine(
        event_bus,
        persistence_path=DATA_ROOT / "world-state-v1.json",
    )
    await world_state.start()
    memory = MemoryRepository(settings.database_path, event_bus)
    await memory.initialize()
    from app.persona_runtime import PersonaRuntime

    persona_runtime = PersonaRuntime(
        settings.database_path,
        event_bus,
        world_state=world_state,
    )
    await persona_runtime.start()
    base_llm = create_llm_provider(settings)
    llm = (
        BrainManager(
            settings.ollama_url,
            settings.llm_model,
            settings.llm_timeout_seconds,
            settings.ollama_keep_alive,
            settings.ollama_context_size,
        )
        if settings.llm_provider == "ollama"
        else base_llm
    )
    warm_manager = OllamaWarmManager(settings, llm, event_bus) if isinstance(llm, BrainManager) else None
    if warm_manager:
        warm_manager.start()
    stt = FasterWhisperSTT(
        model_name=settings.stt_model,
        device=settings.stt_device,
        compute_type=settings.stt_compute_type,
        language=settings.stt_language,
        beam_size=settings.stt_beam_size,
        cpu_threads=settings.stt_cpu_threads,
        workers=settings.stt_workers,
        vad_config=VADConfig(
            enabled=settings.vad_enabled,
            threshold=settings.vad_threshold,
            min_speech_ms=settings.vad_min_speech_ms,
            min_silence_ms=settings.vad_min_silence_ms,
            speech_pad_ms=settings.vad_speech_pad_ms,
        ),
        gain=settings.mic_gain,
    )
    stt_preload_task = asyncio.create_task(_prepare_stt(stt), name="nyra-stt-preload") if settings.conversation_engine else None
    selected_tts_provider = settings.tts_local_engine
    selected_voice = settings.tts_voice
    local_tts = await create_tts_provider(
        selected_tts_provider,
        settings.tts_language,
        settings.tts_model_path,
        settings.tts_voices_path,
        selected_voice,
        settings.chatterbox_python,
        settings.chatterbox_device,
        settings.chatterbox_reference,
        settings.chatterbox_model_id,
        settings.chatterbox_ptbr_model_id,
        settings.chatterbox_resident,
        settings.chatterbox_timeout_seconds,
        False,
        settings.edge_tts_default_locale,
        settings.edge_tts_gender_filter,
        settings.edge_tts_timeout_seconds,
        settings.tts_fallback_provider,
        settings.tts_speaking_rate,
    )
    from app.operator.credentials import CredentialBroker

    credential_broker = CredentialBroker(approvals=None) if settings.credential_broker_enabled else None
    stt = STTProviderRegistry(settings, stt, STTCredentialBroker(credential_broker), event_bus)
    tts_credentials = TtsCredentialBroker(credential_broker)
    tts: TtsProviderRegistry = build_tts_provider_registry(settings, local_tts, tts_credentials)
    catalog = tts_provider_catalog(
        settings.tts_model_path,
        settings.tts_voices_path,
        selected_voice,
        settings.chatterbox_python,
        settings.chatterbox_device,
        settings.chatterbox_reference,
        settings.chatterbox_model_id,
        settings.chatterbox_ptbr_model_id,
        settings.chatterbox_resident,
        settings.chatterbox_timeout_seconds,
        settings.edge_tts_enabled and settings.tts_online_enabled,
        settings.edge_tts_default_locale,
        settings.edge_tts_gender_filter,
        settings.edge_tts_timeout_seconds,
    )
    shell = SystemShellService(settings, event_bus)
    await shell.initialize()
    if credential_broker is not None:
        credential_broker.approvals = shell.approvals
    remote_shell = RemoteShellService(settings, event_bus, shell.approvals)
    await remote_shell.initialize()
    # Cliente Proxmox ÚNICO compartilhado por services e control plane (§46):
    # credenciais aplicadas pela UI atingem toda a aplicação sem restart.
    from app.integrations.proxmox.config import apply_to_runtime as proxmox_apply_runtime

    proxmox = ProxmoxReadOnlyClient(
        settings.proxmox_url,
        settings.proxmox_token_id,
        settings.proxmox_token_secret,
        settings.proxmox_verify_ssl,
    )
    homelab = HomelabControlPlane(settings, event_bus, shell.approvals, remote_shell,
                                  proxmox=proxmox)
    await homelab.initialize()
    tools = create_tool_registry(shell, remote_shell)
    register_homelab_tools(tools, homelab)
    agent = AgentController(settings, event_bus, llm, tools)
    await agent.initialize()
    state_machine = StateMachine(memory, event_bus, persona_runtime)
    speech_queue = SpeechQueue()
    speech_queue.start()
    v4_settings = V4SettingsManager()
    v4_settings.value.realtime.streaming_responses = True
    v4_settings.value.realtime.sentence_streaming = settings.voice_stream_tts
    v4_settings.value.realtime.barge_in = settings.voice_barge_in
    v4_settings.value.realtime.duplex_mode = (
        DuplexMode.SMART_DUPLEX if settings.voice_barge_in else DuplexMode.HALF_DUPLEX
    )
    telemetry = RealtimeTelemetry()
    avatar = AvatarController(event_bus)
    voice_processor = VoiceProcessor(
        v4_settings.value.voice_processor.model_copy(update={"enabled": False})
    )
    perception = PCAwareness(event_bus, v4_settings.value.realtime, v4_settings.value.privacy)
    turn_registry = TurnRegistry()
    orchestrator = RealtimeOrchestrator(
        llm, memory, state_machine, event_bus, tts, speech_queue,
        settings_manager=v4_settings, telemetry=telemetry, perception=perception,
        avatar=avatar, voice_processor=voice_processor,
        turn_registry=turn_registry,
    )
    orchestrator.context.adult_mode = settings.adult_mode_enabled
    orchestrator.tools = tools
    orchestrator.shell = shell
    orchestrator.remote_shell = remote_shell
    orchestrator.agent = agent
    listening = AlwaysListeningManager(settings, event_bus)
    await listening.start()
    conversation = ConversationEngine(
        settings,
        event_bus,
        stt,
        listening,
        orchestrator,
        telemetry,
        warm_manager,
    )
    await conversation.start()

    async def switch_runtime_tts(provider_name: str, voice: str, speaking_rate: float) -> dict:
        del provider_name, voice
        tts.configure(speaking_rate=speaking_rate)
        return {
            "primary": tts.configured_provider,
            "active": tts.active_provider,
            "voice": tts.active_voice,
            "speech_speed": speaking_rate,
        }

    conversation.bind_tts_switcher(switch_runtime_tts)
    network_watch = NetworkWatchMonitor(settings, event_bus)
    await network_watch.initialize()
    orchestrator.network_watch = network_watch
    register_network_watch_tools(tools, network_watch)
    proactive_network = ProactiveNetworkAlerts(
        settings,
        event_bus,
        state_machine,
        speech_queue,
        lambda: orchestrator.tts,
        voice_processor,
    )
    # Detection stays in NetworkWatch; Proactive Presence owns presentation.
    sentinel = SentinelConnector(settings, event_bus)
    await sentinel.initialize()
    orchestrator.sentinel_watch = sentinel
    register_sentinel_tools(tools, sentinel)
    runtime_supervisor = RuntimeSupervisor(
        settings, event_bus,
        process_manager=ProcessManager(),
        history=RuntimeHistory(settings.database_path),
        hooks={"warm_manager": warm_manager, "sentinel": sentinel},
    )
    await runtime_supervisor.initialize()
    register_runtime_tools(tools, runtime_supervisor, shell.approvals)
    desktop = DesktopController(
        event_bus,
        apps_path=settings.desktop_apps_path,
        dynamic_discovery=settings.desktop_dynamic_app_discovery,
        approvals=shell.approvals,
    )
    await desktop.initialize()
    orchestrator.desktop = desktop
    register_desktop_tools(
        tools, desktop,
        uia_enabled=settings.desktop_ui_automation_enabled,
        input_fallback_enabled=settings.desktop_input_fallback_enabled,
    )

    # nyra-7c: pipeline de autonomia do computador (7 camadas, §75).
    from app.computer import (
        ComputerAutonomyService,
        ArtifactContextService,
        ComputerPerceptionService,
        ComputerStateService,
        EffectVerificationService,
        IntentUnderstandingService,
        SkillMemoryService,
        RecentArtifactMemory,
        UsageLearningService,
    )

    computer_state = ComputerStateService(desktop=desktop, world_state=world_state)
    computer_state.load_context()

    def consume_computer_snapshot(snapshot: dict) -> None:
        computer_state.refresh_from_perception(snapshot)
        world_state.ingest_perception_snapshot(
            snapshot,
            user_activity_state=computer_state.user_activity(),
        )

    computer_perception = ComputerPerceptionService(
        event_bus,
        homelab_summary_fn=homelab.status,
        network_status_fn=network_watch.status,
        snapshot_consumer=consume_computer_snapshot,
    )
    computer_intents = IntentUnderstandingService(computer_state)
    effect_verifier = EffectVerificationService()
    usage_learning = UsageLearningService()
    skill_memory = SkillMemoryService(event_bus=event_bus)
    recent_artifacts = RecentArtifactMemory(
        persistence_path=computer_state.base_dir / "recent-artifacts.json",
    )
    artifact_context = ArtifactContextService(
        memory=recent_artifacts, desktop=desktop,
        remote_shell=remote_shell, state=computer_state, event_bus=event_bus,
    )
    computer = ComputerAutonomyService(
        state=computer_state,
        intent_service=computer_intents,
        perception=computer_perception,
        verifier=effect_verifier,
        usage=usage_learning,
        skills=skill_memory,
        desktop=desktop,
        remote_shell=remote_shell,
        artifacts=artifact_context,
    )
    tools.add_result_observer(computer.observe_tool_result)
    tools.add_result_observer(world_state.observe_tool_result)
    orchestrator.computer = computer
    await computer.start_background()

    # USB V1: serviço local usa SetupAPI/ConfigMgr, registry fora do repo e
    # inicia em task própria para não bloquear o restante do startup.
    from app.usb import UsbDeviceService

    usb = UsbDeviceService(event_bus, computer_state=computer_state)
    orchestrator.usb_devices = usb
    from app.usb.hardware import register_hardware_tool
    register_hardware_tool(tools, usb)

    # nyra-full §6/§42: Universal Application Registry — startup leve e
    # refresh periódico em background (nunca bloqueia o event loop).
    universal_refresh_task = asyncio.create_task(_universal_refresh_loop(desktop), name="nyra-universal-apps")
    operator = None
    browser_controller = None
    if settings.local_operator_enabled:
        from app.desktop.browser import BrowserController
        from app.desktop.operator import OperatorController
        from app.desktop.operator_tools import register_browser_tools, register_operator_tools, register_power_tools

        operator = OperatorController(event_bus, shell.approvals)
        register_operator_tools(tools, operator)
        register_power_tools(tools, operator)
        browser_controller = BrowserController()
        register_browser_tools(tools, browser_controller)
    proactive_sentinel = ProactiveSentinelAlerts(
        settings, event_bus, state_machine, speech_queue, lambda: orchestrator.tts, sentinel, memory,
        voice_processor,
    )
    # Sentinel remains the detector; Proactive Presence owns presentation.
    cooldowns = CooldownManager()
    proactive = ProactiveEngine(cooldowns)
    # Operator V2 (prompt9): vision/adapters/browser-v2/credentials/elevated
    # sessions/jobs/tasks/recovery/watcher/workflows/proactive/contexts.
    from app.operator.service import create_operator_v2_service
    from app.operator.tools_reg import register_operator_v2_tools

    operator_v2 = await create_operator_v2_service(
        settings, event_bus, shell.approvals, tools,
        browser_controller=browser_controller if (settings.local_operator_enabled and settings.browser_control_enabled) else None,
        proactive_gate=proactive,
        speech_queue=speech_queue,
        tts_provider_getter=lambda: orchestrator.tts,
        voice_processor=voice_processor,
        credential_broker=credential_broker,
        legacy_monitor_notifications=False,
    )
    await operator_v2.start()
    register_operator_v2_tools(tools, operator_v2)
    orchestrator.monitor_jobs = operator_v2.monitor_jobs
    usb_start_task = asyncio.create_task(usb.start(), name="nyra-usb-start")
    attention = AttentionEngine(event_bus)
    await attention.start()
    reactions = ReactionEngine(event_bus, avatar, perception, proactive, v4_settings.value.realtime)
    await reactions.start()
    skills = create_skill_registry(
        event_bus=event_bus, cooldowns=cooldowns, tools=tools, perception=perception,
        network_watch=network_watch, sentinel=sentinel, memory=memory, listening=listening,
    )
    orchestrator.skills = skills
    await perception.start()
    vtube_studio = VTubeStudioAvatarProvider()
    avatar.attach_provider(vtube_studio)
    await vtube_studio.start()
    from app.emotional_presence import EmotionPresentationCoordinator

    emotional_presence = EmotionPresentationCoordinator(
        event_bus,
        persona_runtime,
        provider_getter=lambda: orchestrator.tts,
        avatar=avatar,
        vtube_studio=vtube_studio,
    )
    orchestrator.emotional_presence = emotional_presence
    await emotional_presence.start()
    voice_hunter = VoiceHunterService(stt=stt, tts_catalog=catalog)
    monitor = HomelabMonitor(settings, tools, event_bus, memory)
    from app.core.lifecycle import coordinate_full_restart
    from app.selfdev import SelfDevelopmentService

    async def selfdev_post_restart_health() -> bool:
        """Use the same essential liveness contract as the root health route."""
        try:
            llm_ok, memory_ok = await asyncio.gather(llm.health(), memory.health())
            return bool(llm_ok and memory_ok)
        except Exception:  # noqa: BLE001 - failed probe must trigger rollback
            return False

    selfdev = SelfDevelopmentService(
        settings,
        event_bus,
        shell,
        llm,
        restart_request=coordinate_full_restart,
        health_check=selfdev_post_restart_health,
    )
    await selfdev.start()
    app.state.services = Services(
        settings=settings,
        event_bus=event_bus,
        memory=memory,
        llm=llm,
        stt=stt,
        tts=tts,
        tts_registry=tts,
        tts_catalog=catalog,
        tools=tools,
        shell=shell,
        remote_shell=remote_shell,
        agent=agent,
        state_machine=state_machine,
        orchestrator=orchestrator,
        monitor=monitor,
        homelab=homelab,
        proxmox=proxmox,
        speech_queue=speech_queue,
        listening=listening,
        network_watch=network_watch,
        proactive_network=proactive_network,
        sentinel=sentinel,
        proactive_sentinel=proactive_sentinel,
        voice_hunter=voice_hunter,
        v4_settings=v4_settings,
        telemetry=telemetry,
        perception=perception,
        attention=attention,
        reactions=reactions,
        proactive=proactive,
        avatar=avatar,
        vtube_studio=vtube_studio,
        skills=skills,
        voice_processor=voice_processor,
        brain=llm,
        warm_manager=warm_manager,
        conversation=conversation,
        runtime_supervisor=runtime_supervisor,
        selfdev=selfdev,
        desktop=desktop,
        operator=operator,
        operator_v2=operator_v2,
        turns=turn_registry,
        computer=computer,
        computer_state=computer_state,
        computer_perception=computer_perception,
        usage_learning=usage_learning,
        skill_memory=skill_memory,
        usb=usb,
        world_state=world_state,
        persona_runtime=persona_runtime,
        emotional_presence=emotional_presence,
    )
    # Intelligence Platform V2 compõe as autoridades existentes; não substitui
    # Approval Gate, Tool Registry, Credential Broker ou operadores grounded.
    from app.intelligence import IntelligencePlatform

    intelligence = IntelligencePlatform(app.state.services)
    app.state.services.intelligence = intelligence
    await intelligence.initialize()
    persona_runtime.bind_memory_v2(intelligence.memory)
    orchestrator.context.intelligence = intelligence
    orchestrator.open_loops = intelligence.open_loops
    orchestrator.world_state = world_state

    from app.hardware_engine.service import HardwareEngineeringService
    from app.hardware_engine.tools import register_tools as register_hardware_engine_tools
    from app.core.paths import DATA_ROOT as hardware_data_root
    hardware_engine = HardwareEngineeringService(app.state.services, hardware_data_root / 'hardware-engine')
    app.state.services.hardware_engine = hardware_engine
    orchestrator.hardware_engine = hardware_engine
    await hardware_engine.initialize()
    register_hardware_engine_tools(tools, hardware_engine)

    from app.proactive_presence import ProactivePresenceService

    proactive_presence = ProactivePresenceService(
        settings=settings,
        event_bus=event_bus,
        intelligence_store=intelligence.store,
        world_state=world_state,
        open_loops=intelligence.open_loops,
        speech_queue=speech_queue,
        provider_getter=lambda: orchestrator.tts,
        voice_processor=voice_processor,
        persona_runtime=persona_runtime,
        emotional_presence=emotional_presence,
    )
    app.state.services.proactive_presence = proactive_presence
    await proactive_presence.start()
    intelligence.capabilities.register(
        "proactive_presence_engine",
        "Apresentação proativa relevante, persistente e limitada por policy.",
        lambda: {"state": "AVAILABLE", "health": "READY"},
        dependencies=("world_state_engine", "open_loops_engine", "action_budget"),
    )
    intelligence.capabilities.register(
        "persona_emotional_runtime",
        "Identidade, relacionamento, emoção contextual e policy de diálogo compartilhados.",
        lambda: {"state": "AVAILABLE", "health": "READY"},
        dependencies=("memory_v2", "world_state_engine", "proactive_presence_engine"),
    )
    intelligence.capabilities.register(
        "emotional_presence_sync",
        "Sincroniza o estado emocional único entre texto, voz e o modelo atual do VTube Studio.",
        lambda: {"state": "AVAILABLE", "health": "READY"},
        dependencies=("persona_emotional_runtime", "proactive_presence_engine"),
    )

    operator_tasks = await operator_v2.tasks.list_tasks(include_terminal=False)
    intelligence_tasks = await intelligence.tasks.list(include_terminal=False)
    monitor_jobs = await operator_v2.monitor_jobs.list(include_terminal=False)
    await intelligence.open_loops.reconcile(
        tasks=[
            *(operator_tasks.get("tasks") or []),
            *(task.model_dump(mode="json") for task in intelligence_tasks),
        ],
        monitors=monitor_jobs.get("monitors") or [],
        artifacts=(item.model_dump(mode="json") for item in recent_artifacts.items),
    )
    integration_config = homelab.configuration_status()
    world_state.synchronize_authorities(
        tasks=[
            *(operator_tasks.get("tasks") or []),
            *(task.model_dump(mode="json") for task in intelligence_tasks),
        ],
        monitors=monitor_jobs.get("monitors") or [],
        artifacts=(item.model_dump(mode="json") for item in recent_artifacts.items),
        integrations={
            "proxmox": {"state": integration_config.get("proxmox", "UNKNOWN")},
            "openwrt": {"state": integration_config.get("openwrt", "UNKNOWN")},
            "home_assistant": {"state": integration_config.get("home_assistant", "UNKNOWN")},
            "sentinel": sentinel.status(),
        },
        network=network_watch.status(),
    )
    # VoiceProcessorBridge (prompt11 §120): lazy-friendly singleton no estado.
    from app.speech.external_bridge import VoiceProcessorBridge

    voice_bridge = VoiceProcessorBridge(settings)
    app.state.services.voice_bridge = voice_bridge
    if settings.voice_processor_bridge_enabled:
        # Refresh periódico: cached_status acompanha o processor externo de
        # verdade (READY quando o Satellite sobe, OFFLINE + fallback quando cai).
        voice_bridge.start_background_refresh()
    # prompt11_1 §59: após restart, perfil HA ativo + credenciais Proxmox são
    # restaurados a partir das fontes persistentes (perfil/Broker), sem secrets
    # em settings e sem nova implementação para validar contra o real depois.
    from app.integrations.home_assistant_profiles import (
        apply_active_profile_to_runtime,
    )

    ha_restore = apply_active_profile_to_runtime(app.state.services)
    if ha_restore.get("applied"):
        logger.info("ha_active_profile_restored",
                    extra={"profile": str(ha_restore.get("active_profile"))})
    proxmox_apply_summary = proxmox_apply_runtime(app.state.services)
    if proxmox_apply_summary.get("applied"):
        logger.info("proxmox_config_restored",
                    extra={"auth": bool(proxmox_apply_summary.get("auth_configured"))})
    monitor.start()
    homelab.start()
    from app.benchmark import ModelBenchmarkLab

    app.state.benchmark_lab = ModelBenchmarkLab(
        settings.ollama_url,
        brain=llm if isinstance(llm, BrainManager) else None,
        settings=settings,
    )
    logger.info(
        "nyra_started",
        extra={"llm": llm.name, "model": settings.llm_model, "tts": tts.name, "stt": stt.name},
    )
    yield
    await _graceful_shutdown(locals())


_SHUTDOWN_STEP_TIMEOUT_SECONDS = 15.0


_UNIVERSAL_REFRESH_INTERVAL_SECONDS = 6 * 3600


async def _universal_refresh_loop(desktop) -> None:
    """nyra-full §42: load → lightweight verification → periodic background refresh."""
    try:
        await asyncio.to_thread(desktop.universal.refresh, True)
    except Exception as error:  # noqa: BLE001 - índice nunca derruba o backend
        logger.warning(
            "universal_registry_initial_refresh_failed type=%s", type(error).__name__
        )
    while True:
        await asyncio.sleep(_UNIVERSAL_REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(desktop.universal.refresh, True)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "universal_registry_periodic_refresh_failed type=%s", type(error).__name__
            )


async def _bounded_step(label: str, operation) -> None:
    """Run one shutdown step without ever blocking the whole shutdown.

    A step that ignores cancellation or awaits a dead-loop primitive must not
    wedge the process: we abandon it after the timeout and keep going.
    """
    task = asyncio.ensure_future(operation())
    done, pending = await asyncio.wait({task}, timeout=_SHUTDOWN_STEP_TIMEOUT_SECONDS)
    if pending:
        task.cancel()
        logger.error("shutdown_step_timeout", extra={"step": label, "timeout": _SHUTDOWN_STEP_TIMEOUT_SECONDS})
        return
    error = task.exception()
    if error is not None and not isinstance(error, asyncio.CancelledError):
        logger.error("shutdown_step_failed", extra={"step": label, "error_type": type(error).__name__})


async def _graceful_shutdown(scope: dict) -> None:
    logger.info("shutdown_started")
    operator_v2 = scope["operator_v2"]
    runtime_supervisor = scope["runtime_supervisor"]
    selfdev = scope.get("selfdev")
    conversation = scope["conversation"]
    warm_manager = scope.get("warm_manager")
    stt_preload_task = scope.get("stt_preload_task")
    voice_hunter = scope["voice_hunter"]
    universal_refresh_task = scope.get("universal_refresh_task")
    usb = scope.get("usb")
    usb_start_task = scope.get("usb_start_task")
    computer = scope.get("computer")

    async def _stop_parent_watch() -> None:
        parent_watch = scope.get("parent_watch")
        if parent_watch is not None:
            await asyncio.to_thread(parent_watch.stop)
    if computer is not None:
        # nyra-7c §83: persistência atômica de contexto/usage/skills.
        await _bounded_step("computer_autonomy_shutdown", computer.shutdown)

    async def _cancel_universal() -> None:
        if universal_refresh_task and not universal_refresh_task.done():
            universal_refresh_task.cancel()
            try:
                await universal_refresh_task
            except asyncio.CancelledError:
                pass

    async def _cancel_preload() -> None:
        if stt_preload_task and not stt_preload_task.done():
            stt_preload_task.cancel()
            try:
                await stt_preload_task
            except asyncio.CancelledError:
                pass

    async def _cancel_voice_hunter() -> None:
        if voice_hunter._task and not voice_hunter._task.done():
            await voice_hunter.cancel()

    async def _stop_usb() -> None:
        if usb_start_task and not usb_start_task.done():
            try:
                await usb_start_task
            except asyncio.CancelledError:
                pass
        if usb is not None:
            await usb.stop()

    steps = [
        ("parent_watch.stop", _stop_parent_watch if scope.get("parent_watch") else None),
        ("proactive_presence.stop", scope.get("proactive_presence").stop if scope.get("proactive_presence") else None),
        ("hardware_engine.stop", scope.get("hardware_engine").stop if scope.get("hardware_engine") else None),
        ("intelligence.stop", scope.get("intelligence").stop if scope.get("intelligence") else None),
        ("selfdev.stop", selfdev.stop if selfdev else None),
        ("usb.stop", _stop_usb),
        ("operator_v2.stop", operator_v2.stop),
        ("runtime_supervisor.shutdown", runtime_supervisor.shutdown),
        ("conversation.stop", conversation.stop),
        ("stt.close", scope["stt"].close if "stt" in scope else None),
        ("warm_manager.stop", warm_manager.stop if warm_manager else None),
        ("stt_preload.cancel", _cancel_preload),
        ("voice_hunter.cancel", _cancel_voice_hunter),
        ("voice_bridge.refresh.stop",
         scope["voice_bridge"].stop_background_refresh
         if "voice_bridge" in scope else None),
        ("perception.stop", scope["perception"].stop),
        ("emotional_presence.stop", scope["emotional_presence"].stop if scope.get("emotional_presence") else None),
        ("vtube_studio.stop", scope["vtube_studio"].stop),
        ("reactions.stop", scope["reactions"].stop),
        ("attention.stop", scope["attention"].stop),
        ("monitor.stop", scope["monitor"].stop),
        ("homelab.stop", scope["homelab"].stop),
        ("proactive_sentinel.stop", scope["proactive_sentinel"].stop),
        ("sentinel.shutdown", scope["sentinel"].shutdown),
        ("proactive_network.stop", scope["proactive_network"].stop),
        ("network_watch.stop", scope["network_watch"].stop),
        ("listening.stop", scope["listening"].stop),
        ("speech_queue.stop", scope["speech_queue"].stop),
        ("tts.close", scope["tts"].close if "tts" in scope else None),
        ("persona_runtime.stop", scope["persona_runtime"].stop if scope.get("persona_runtime") else None),
        ("world_state.stop", scope["world_state"].stop if scope.get("world_state") else None),
    ]
    for label, operation in steps:
        if operation is None:
            continue
        try:
            await _bounded_step(label, operation)
        except Exception as error:  # noqa: BLE001 - shutdown must always complete
            logger.error("shutdown_step_error", extra={"step": label, "error_type": type(error).__name__})
    logger.info("shutdown_complete")


app = FastAPI(
    title="NYRA Local AI",
    version=APP_VERSION,
    description="Identidade + LLM + memória + percepção + voz + avatar + ferramentas + eventos",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{settings.frontend_port}",
        f"http://localhost:{settings.frontend_port}",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
app.add_middleware(
    LocalRequestSecurityMiddleware,
    frontend_port=settings.frontend_port,
    backend_port=settings.backend_port,
)
app.include_router(router)
app.include_router(stt_router)
app.include_router(intelligence_router, prefix="/api")
from app.hardware_engine.api import router as hardware_engine_router
app.include_router(hardware_engine_router, prefix="/api")
app.include_router(proactive_presence_router, prefix="/api")
app.include_router(persona_runtime_router, prefix="/api")
app.include_router(emotional_presence_router, prefix="/api")
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.post("/internal/owned-shutdown", include_in_schema=False)
async def owned_backend_shutdown(request: Request) -> dict:
    """Private one-shot shutdown signal from the owning Tauri process."""
    expected = os.environ.get("NYRA_OWNER_TOKEN", "")
    supplied = request.headers.get("x-nyra-owner-token", "")
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"} or not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=404, detail="Not found")
    from app.core.lifecycle import coordinate_full_shutdown

    return await coordinate_full_shutdown("desktop_owner_exit")


@app.get("/health", include_in_schema=False)
async def root_health():
    """Compatibility health route requested by the MVP contract."""
    services = app.state.services
    llm_ok, llm_ready, memory_ok, stt_ok, tts_ok = await asyncio.gather(
        services.llm.health(),
        services.llm.ready(),
        services.memory.health(),
        services.stt.health(),
        services.tts.health(),
    )
    proactive_status = (
        await services.proactive_presence.status()
        if services.proactive_presence is not None else None
    )
    persona_status = (
        await services.persona_runtime.status()
        if services.persona_runtime is not None else None
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
        "homelab": services.homelab.status(),
        "agent": services.agent.status(),
        "sentinel_watch": {
            "enabled": services.sentinel.settings.sentinel_watch_enabled,
            "state": services.sentinel.state.value.casefold(),
        },
        "realtime": services.conversation.state.value,
        "perception": services.perception.snapshot.enabled,
        "world_state": services.world_state.health() if services.world_state else None,
        "proactive_presence": proactive_status,
        "persona_runtime": persona_status,
        "emotional_presence": await services.emotional_presence.status() if services.emotional_presence else None,
    }
