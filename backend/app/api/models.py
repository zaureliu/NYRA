from pydantic import BaseModel, Field

from app.speech.profile import VoiceSynthesisOptions
from app.speech.pronunciation.models import PronunciationRule
from app.listening.models import ListeningLeaseRequest, ListeningSettingsUpdate, PlaybackStateRequest
from app.network_watch.models import NetworkDebugRequest, NetworkWatchSettingsUpdate
from app.integrations.sentinel.models import SentinelDebugRequest, SentinelSettingsUpdate, SentinelTokenUpdate
from app.realtime.models import RealtimeSettingsUpdate
from app.speech.voice_processor import VoiceProcessorConfig
from app.brain import BrainBenchmarkRequest, BrainSelectionRequest
from app.avatar.vtube_studio.models import VTSSettingsUpdate
from app.tools.shell_models import ShellApprovalDecision
from app.conversation import AudioSettingsUpdate, InterruptionRequest


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    synthesize: bool = True
    turn_id: str | None = Field(
        default=None,
        pattern=r"^turn_[0-9a-f]{8,64}$",
        description="Identificador imutável do turno; gerado pelo backend quando ausente.",
    )


class RuntimeActionRequest(BaseModel):
    approval_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(default="", max_length=500)


class DesktopOpenRequest(BaseModel):
    query: str = Field(min_length=2, max_length=80, pattern=r"^[\w\s.\-()&+]+$")


class ToolExecutionRequest(BaseModel):
    parameters: dict = Field(default_factory=dict)


class SkillExecutionRequest(BaseModel):
    parameters: dict = Field(default_factory=dict)
    confirmed: bool = False


class SkillSettingsUpdate(BaseModel):
    enabled: bool | None = None
    cooldown_seconds: float | None = Field(None, ge=0, le=86400)


class VoiceProcessorRequest(BaseModel):
    audio_url: str = Field(min_length=12, max_length=300)
    state: str = Field(default="neutral", pattern=r"^(neutral|happy|curious|focused|concerned|amused|tired|surprised|alert)$")


class Live2DLipSyncRequest(BaseModel):
    value: float = Field(ge=0.0, le=1.0)


class Live2DCursorRequest(BaseModel):
    x: float = Field(ge=-1.0, le=1.0)
    y: float = Field(ge=-1.0, le=1.0)


class ImportanceUpdate(BaseModel):
    importance: int = Field(ge=1, le=10)


class VoiceLabRequest(VoiceSynthesisOptions):
    provider: str = Field(pattern=r"^(chatterbox|chatterbox_multilingual_v3|chatterbox_ptbr|kokoro|edge_tts)$")
    text: str = Field(min_length=1, max_length=4000)
    state: str = Field(default="neutral", pattern=r"^(neutral|happy|curious|focused|concerned|amused|tired|surprised)$")


class VoiceProfileUpdate(VoiceSynthesisOptions):
    provider: str = Field(pattern=r"^(chatterbox|chatterbox_multilingual_v3|chatterbox_ptbr|kokoro|edge_tts)$")


class PronunciationPreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    provider: str = Field(default="default", pattern=r"^(default|kokoro|chatterbox|chatterbox_multilingual_v3|chatterbox_ptbr|edge_tts)$")
    literal_required: bool = False


class PronunciationRuleRequest(PronunciationRule):
    pass


class AdultModeRequest(BaseModel):
    enabled: bool
    confirmed_18_plus: bool = False


class VoiceHunterPreviewRequest(BaseModel):
    phrase: str = Field(default="casual", pattern=r"^(casual|natural|curiosa|humor_seco|tecnica|rede|alerta|long_form)$")
    text: str | None = Field(default=None, min_length=1, max_length=4000)


class VoiceHunterCompareRequest(BaseModel):
    candidate_a: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    candidate_b: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    phrase: str = Field(default="casual", pattern=r"^(casual|natural|curiosa|humor_seco|tecnica|rede|alerta|long_form)$")


class VoiceHunterPreferenceRequest(BaseModel):
    favorite: bool | None = None
    discarded: bool | None = None
    rating: float | None = Field(default=None, ge=0, le=10)


__all__ = [
    "ChatRequest", "ToolExecutionRequest", "ImportanceUpdate", "VoiceLabRequest",
    "VoiceProfileUpdate", "PronunciationPreviewRequest", "PronunciationRuleRequest",
    "AdultModeRequest", "ListeningLeaseRequest", "ListeningSettingsUpdate",
    "PlaybackStateRequest", "NetworkDebugRequest", "NetworkWatchSettingsUpdate",
    "SentinelDebugRequest", "SentinelSettingsUpdate", "SentinelTokenUpdate",
    "VoiceHunterPreviewRequest", "VoiceHunterCompareRequest", "VoiceHunterPreferenceRequest",
    "RealtimeSettingsUpdate", "VoiceProcessorConfig", "VoiceProcessorRequest",
    "SkillExecutionRequest", "SkillSettingsUpdate",
    "BrainBenchmarkRequest", "BrainSelectionRequest",
    "VTSSettingsUpdate", "Live2DLipSyncRequest", "Live2DCursorRequest",
    "ShellApprovalDecision",
    "AudioSettingsUpdate", "InterruptionRequest",
]
