from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.character.state import StateMachine
from app.core.config import Settings
from app.core.turn import TurnRegistry
from app.events import EventBus
from app.homelab import HomelabControlPlane, HomelabMonitor
from app.listening import AlwaysListeningManager
from app.network_watch import NetworkWatchMonitor
from app.network_watch.alerts import ProactiveNetworkAlerts
from app.integrations.sentinel import ProactiveSentinelAlerts, SentinelConnector
from app.integrations.proxmox import ProxmoxReadOnlyClient
from app.llm import LLMProvider
from app.memory import MemoryRepository
from app.orchestrator import ChatOrchestrator
from app.speech.stt import STTProvider
from app.speech.tts import TTSProvider
from app.speech.queue import SpeechQueue
from app.tools import RemoteShellService, SystemShellService, ToolRegistry
from app.agent import AgentController
from app.voice_hunter import VoiceHunterService
from app.realtime.orchestrator import RealtimeOrchestrator
from app.realtime.settings import V4SettingsManager
from app.realtime.telemetry import RealtimeTelemetry
from app.perception import PCAwareness
from app.attention import AttentionEngine
from app.reactions import ReactionEngine
from app.proactive import ProactiveEngine
from app.avatar import AvatarController, VTubeStudioAvatarProvider
from app.skills import SkillRegistry
from app.speech.voice_processor import VoiceProcessor
from app.brain import BrainManager
from app.conversation import ConversationEngine
from app.llm.warm_manager import OllamaWarmManager
from app.runtime import RuntimeSupervisor
from app.desktop import DesktopController
from app.selfdev import SelfDevelopmentService

if TYPE_CHECKING:
    from app.desktop.operator import OperatorController
    from app.operator.service import OperatorV2Service


@dataclass
class Services:
    settings: Settings
    event_bus: EventBus
    memory: MemoryRepository
    llm: LLMProvider
    stt: STTProvider
    tts: TTSProvider
    tts_catalog: list[TTSProvider]
    tools: ToolRegistry
    shell: SystemShellService
    remote_shell: RemoteShellService
    agent: AgentController
    state_machine: StateMachine
    orchestrator: RealtimeOrchestrator
    monitor: HomelabMonitor
    homelab: HomelabControlPlane
    proxmox: ProxmoxReadOnlyClient
    speech_queue: SpeechQueue
    listening: AlwaysListeningManager
    network_watch: NetworkWatchMonitor
    proactive_network: ProactiveNetworkAlerts
    sentinel: SentinelConnector
    proactive_sentinel: ProactiveSentinelAlerts
    voice_hunter: VoiceHunterService
    v4_settings: V4SettingsManager
    telemetry: RealtimeTelemetry
    perception: PCAwareness
    attention: AttentionEngine
    reactions: ReactionEngine
    proactive: ProactiveEngine
    avatar: AvatarController
    vtube_studio: VTubeStudioAvatarProvider
    skills: SkillRegistry
    voice_processor: VoiceProcessor
    brain: BrainManager
    warm_manager: OllamaWarmManager | None
    conversation: ConversationEngine
    runtime_supervisor: RuntimeSupervisor
    selfdev: SelfDevelopmentService
    desktop: DesktopController
    operator: "OperatorController | None"
    operator_v2: "OperatorV2Service | None"
    turns: TurnRegistry
    # nyra-7c: camadas de autonomia do computador (pipeline unificado).
    computer: Any = None
    computer_state: Any = None
    computer_perception: Any = None
    usage_learning: Any = None
    skill_memory: Any = None
    usb: Any = None
    # Plataforma integrada V2; inicializada após o container reunir as
    # autoridades legadas de policy, tools, capabilities e SelfDev.
    intelligence: Any = None
