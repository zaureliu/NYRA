from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import CONFIG_ROOT, DATA_ROOT, PROJECT_ROOT, resolve_packaged_path
from app.speech.recognition.models import STTSettings
from app.speech.synthesis_config import UniversalTtsSettings


def _private_config_path(local_name: str, public_name: str) -> Path:
    """Prefer ignored operator config and fall back to a safe public template."""
    local_path = CONFIG_ROOT / local_name
    return local_path if local_path.is_file() else CONFIG_ROOT / public_name


def _yaml_defaults() -> dict[str, Any]:
    path = CONFIG_ROOT / "default.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NYRA_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    host: str = "127.0.0.1"
    backend_port: int = Field(8000, ge=1, le=65535)
    frontend_port: int = Field(5173, ge=1, le=65535)
    log_level: str = "INFO"
    database_path: Path = Path("data/nyra.db")

    llm_provider: Literal["ollama", "mock"] = "ollama"
    llm_model: str = "qwen3:8b"
    ollama_url: str = "http://127.0.0.1:11434"
    llm_timeout_seconds: float = Field(180, ge=1, le=600)
    ollama_preload: bool = True
    ollama_keep_alive: str = "1h"
    ollama_warmup: bool = True
    ollama_context_size: int = Field(8192, ge=2048, le=32768)
    ollama_preload_timeout_seconds: int = Field(300, ge=10, le=900)
    ollama_recovery_interval_seconds: int = Field(10, ge=2, le=300)
    ollama_unload_previous_model: bool = True

    stt_provider: Literal["faster_whisper", "disabled"] = "faster_whisper"
    stt_model: str = "tiny"
    stt_device: Literal["cpu", "cuda", "auto"] = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "pt"
    stt_beam_size: int = Field(1, ge=1, le=10)
    stt_cpu_threads: int = Field(4, ge=0, le=64)
    stt_workers: int = Field(1, ge=1, le=8)
    stt_recognition: STTSettings = Field(default_factory=STTSettings)
    silence_threshold: float = Field(0.018, ge=0, le=1)
    tts_provider: Literal["auto", "chatterbox", "xtts", "kokoro", "pyttsx3", "edge_tts", "disabled"] = "edge_tts"
    # Logical provider layer. The legacy tts_provider remains the Voice Lab
    # engine selector; runtime conversation routes through this local-first layer.
    tts_provider_id: Literal["local", "openai", "elevenlabs", "gradium", "custom"] = "local"
    tts_universal: UniversalTtsSettings = Field(default_factory=UniversalTtsSettings)
    tts_provider_fallback: Literal["local"] = "local"
    tts_online_enabled: bool = False
    tts_local_engine: Literal["auto", "chatterbox", "chatterbox_multilingual_v3", "chatterbox_ptbr", "xtts", "kokoro", "pyttsx3", "disabled"] = "kokoro"
    tts_openai_model: str = "gpt-4o-mini-tts"
    tts_openai_voice: str = "coral"
    tts_elevenlabs_model: str = "eleven_multilingual_v2"
    tts_elevenlabs_voice_id: str = ""
    tts_online_timeout_seconds: float = Field(30, ge=5, le=120)
    tts_language: str = "pt-BR"
    tts_model_path: Path = Path("data/models/kokoro-v1.0.int8.onnx")
    tts_voices_path: Path = Path("data/models/voices-v1.0.bin")
    tts_voice: str = "en-US-AvaMultilingualNeural"
    tts_voice_identity_version: str = "ava-v1"
    tts_speaking_rate: float = Field(0.97, ge=0.7, le=1.3)
    voice_emotion_mode: Literal["automatic", "neutral_only"] = "automatic"
    voice_expressiveness: Literal["low", "normal", "high"] = "normal"
    tts_fallback_provider: Literal["pyttsx3", "edge_tts", "disabled"] = "pyttsx3"
    chatterbox_python: Path = Path(".venv-chatterbox/Scripts/python.exe")
    chatterbox_device: Literal["cpu", "cuda", "mps"] = "cpu"
    chatterbox_reference: Path = Path("data/voices/nyra_reference.wav")
    chatterbox_model_id: str = "ResembleAI/chatterbox"
    chatterbox_ptbr_model_id: str = "ResembleAI/Chatterbox-Multilingual-pt-br"
    chatterbox_resident: bool = True
    chatterbox_timeout_seconds: int = Field(900, ge=30, le=3600)
    edge_tts_enabled: bool = True
    edge_tts_default_locale: str = "pt-BR"
    edge_tts_gender_filter: str = "Female"
    edge_tts_timeout_seconds: int = Field(30, ge=5, le=120)
    pronunciation_enabled: bool = True
    pronunciation_provider_overrides: bool = True
    pronunciation_normalize_technical_numbers: bool = True
    pronunciation_read_full_urls: bool = False
    pronunciation_read_full_paths: bool = False
    pronunciation_unknown_detection: bool = True
    pronunciation_debug: bool = False
    adult_mode_enabled: bool = False
    microphone: str = "default"
    speaker: str = "default"
    audio_volume: float = Field(0.9, ge=0, le=1)
    mic_gain: float = Field(1.0, ge=0.25, le=4)
    mic_preroll_ms: int = Field(250, ge=0, le=1000)
    mic_postroll_ms: int = Field(850, ge=200, le=2000)
    voice_speech_start_ms: int = Field(100, ge=40, le=1000)
    voice_max_utterance_seconds: int = Field(60, ge=5, le=300)
    vad_enabled: bool = True
    vad_threshold: float = Field(0.5, ge=0, le=1)
    vad_min_speech_ms: int = Field(250, ge=50, le=5000)
    vad_min_silence_ms: int = Field(650, ge=100, le=5000)
    vad_speech_pad_ms: int = Field(250, ge=0, le=1000)
    keep_debug_audio: bool = False

    always_listening_enabled: bool = True
    listening_mode: Literal["push_to_talk", "wake_word", "hands_free"] = "hands_free"
    wake_word: str = "Nyra"
    hands_free_timeout_seconds: int = Field(120, ge=15, le=3600)
    listening_guard_ms: int = Field(400, ge=100, le=3000)
    listening_privacy_indicator: bool = True
    listening_audio_debug: bool = False
    conversation_engine: bool = True
    natural_conversation_enabled: bool = True
    voice_barge_in: bool = True
    voice_stream_tts: bool = True

    # ---- Voice Processor Bridge (prompt11 Parte W §120-§129)
    voice_processor_bridge_enabled: bool = False
    voice_processor_bridge_protocol: str = "http"
    voice_processor_bridge_endpoint: str = "http://127.0.0.1:8977"
    voice_processor_bridge_autostart: bool = False
    voice_profile_active: str = ""

    network_watch_enabled: bool = False
    network_voice_alerts: bool = True
    network_desktop_alerts: bool = True
    network_quiet_mode: bool = False
    network_critical_voice_in_quiet: bool = False
    network_interface_interval: float = Field(1.0, ge=0.5, le=60)
    network_gateway_interval: float = Field(2.0, ge=1, le=300)
    network_internet_interval: float = Field(5.0, ge=2, le=600)
    network_dns_interval: float = Field(15.0, ge=5, le=3600)
    network_http_interval: float = Field(30.0, ge=10, le=3600)
    network_latency_warning_ms: float = Field(100, ge=10, le=5000)
    network_latency_critical_ms: float = Field(200, ge=20, le=10000)
    network_packet_loss_warning: float = Field(5, ge=0, le=100)
    network_packet_loss_critical: float = Field(15, ge=0, le=100)
    network_jitter_warning_ms: float = Field(40, ge=1, le=5000)
    network_alert_cooldown_seconds: int = Field(300, ge=10, le=86400)
    network_history_retention_days: int = Field(30, ge=1, le=365)
    network_dns_target: str = "cloudflare.com"
    network_internet_targets: str = "1.1.1.1:443,8.8.8.8:53"

    shell_enabled: bool = True
    shell_default: Literal["powershell", "cmd"] = "powershell"
    shell_timeout_seconds: int = Field(30, ge=1, le=300)
    shell_max_timeout_seconds: int = Field(300, ge=1, le=3600)
    shell_max_output_chars: int = Field(50_000, ge=1_000, le=1_000_000)
    shell_max_calls_per_turn: int = Field(10, ge=1, le=50)
    shell_confirm_destructive: bool = True
    shell_approval_ttl_seconds: int = Field(300, ge=30, le=3600)
    shell_default_working_directory: Path = PROJECT_ROOT

    remote_shell_enabled: bool = True
    ssh_connect_timeout_seconds: int = Field(5, ge=1, le=60)
    ssh_command_timeout_seconds: int = Field(30, ge=1, le=300)
    ssh_max_timeout_seconds: int = Field(300, ge=1, le=3600)
    ssh_max_output_chars: int = Field(50_000, ge=1_000, le=1_000_000)
    trusted_hosts_path: Path = Field(
        default_factory=lambda: _private_config_path("network_aliases.local.json", "network_aliases.json")
    )

    agent_enabled: bool = True
    agent_max_steps: int = Field(12, ge=1, le=50)
    agent_max_tool_calls: int = Field(20, ge=1, le=100)
    agent_max_runtime_seconds: int = Field(300, ge=10, le=3600)
    agent_read_only: bool = False
    agent_auto_remediation: bool = True
    agent_auto_remediation_actions: str = ""
    agent_max_identical_repeats: int = Field(2, ge=1, le=10)
    agent_max_consecutive_failures: int = Field(3, ge=1, le=10)

    # Self-Development Engine. Publicação automática permanece opt-in.
    selfdev_mode: Literal["OFF", "OBSERVE_ONLY", "AUTONOMOUS_SAFE", "AUTONOMOUS_ADVANCED"] = "AUTONOMOUS_SAFE"
    selfdev_model: str = "qwen3:8b"
    selfdev_workspace: Path = PROJECT_ROOT.parent / "Nyra-Auto-Code"
    selfdev_canonical_root: Path = PROJECT_ROOT
    selfdev_public_snapshot: Path = PROJECT_ROOT.parent / "NYRA-GitHub-Public"
    selfdev_run_when_idle: bool = True
    selfdev_auto_publish_github: bool = False
    selfdev_max_auto_promotions_per_day: int = Field(3, ge=0, le=20)
    selfdev_max_candidate_runtime_minutes: int = Field(30, ge=1, le=240)
    selfdev_max_files_low_risk: int = Field(8, ge=1, le=100)
    selfdev_max_diff_lines_low_risk: int = Field(500, ge=1, le=20_000)
    selfdev_cooldown_minutes: int = Field(15, ge=0, le=1440)

    runtime_supervisor_enabled: bool = True
    runtime_services_path: Path = PROJECT_ROOT / "config" / "runtime_services.yaml"
    runtime_health_interval_seconds: int = Field(15, ge=5, le=600)
    runtime_default_startup_timeout_seconds: int = Field(30, ge=1, le=300)
    runtime_max_restarts: int = Field(3, ge=1, le=20)
    runtime_restart_window_seconds: int = Field(600, ge=30, le=3600)
    runtime_log_tail_lines: int = Field(100, ge=1, le=1000)
    runtime_log_max_chars: int = Field(50_000, ge=1_000, le=200_000)
    runtime_alert_cooldown_seconds: int = Field(120, ge=10, le=3_600)
    runtime_auto_recovery_services: str = ""

    desktop_apps_path: Path = PROJECT_ROOT / "config" / "desktop_apps.yaml"

    turn_isolation_enabled: bool = True
    conversation_single_active_turn: bool = True
    desktop_dynamic_app_discovery: bool = True
    elevated_broker_enabled: bool = True
    local_operator_enabled: bool = True
    desktop_ui_automation_enabled: bool = True
    desktop_input_fallback_enabled: bool = True

    sentinel_watch_enabled: bool = False
    sentinel_auto_discovery: bool = True
    sentinel_discovery_interval: int = Field(60, ge=15, le=3600)
    sentinel_host: str = ""
    sentinel_port: int = Field(5000, ge=1, le=65535)
    sentinel_prefer_manual_host: bool = True
    sentinel_voice_alerts: bool = True
    sentinel_desktop_alerts: bool = True
    sentinel_critical_only: bool = False
    sentinel_store_event_history: bool = True
    sentinel_create_episodic_memory: bool = False
    sentinel_auto_reconnect: bool = True
    sentinel_reconnect_backoff: str = "1,2,5,10,30,60"
    sentinel_debug_mode: bool = False
    sentinel_discovery_allowlist: str = ""
    sentinel_event_retention_days: int = Field(30, ge=1, le=365)
    sentinel_alert_cooldown_seconds: int = Field(300, ge=10, le=86400)
    sentinel_disconnect_grace_seconds: int = Field(7, ge=1, le=60)
    sentinel_bridge_token: str = ""

    desktop_always_on_top: bool = True
    desktop_click_through: bool = False
    desktop_overlay_scale: float = Field(1.0, ge=0.5, le=2)
    desktop_speech_bubble: bool = True
    desktop_idle_animation: bool = True
    desktop_start_with_windows: bool = False

    # ---- Operator V2 flags (prompt9 Parte R §281-§282: toda flag tem consumer + teste)
    vision_enabled: bool = True
    vision_frame_ttl_seconds: int = Field(45, ge=5, le=300)
    vision_debug_keep_frames: bool = False
    browser_control_enabled: bool = True
    credential_broker_enabled: bool = True
    persistent_jobs_enabled: bool = True
    workflow_engine_enabled: bool = True
    desktop_watcher_enabled: bool = True
    watch_default_ttl_seconds: int = Field(300, ge=15, le=3600)
    watchdog_enabled: bool = True
    watchdog_heartbeat_path: Path = DATA_ROOT / "watchdog-heartbeat.json"
    proactive_operator_enabled: bool = False
    proactive_presence_enabled: bool = True
    proactive_presence_mode: Literal["NORMAL", "QUIET", "DO_NOT_DISTURB"] = "NORMAL"
    proactive_voice_enabled: bool = False
    proactive_presence_cooldown_seconds: int = Field(300, ge=10, le=86400)
    proactive_presence_max_per_hour: int = Field(6, ge=1, le=60)
    proactive_presence_defer_ttl_seconds: int = Field(1800, ge=30, le=86400)
    elevated_session_default_ttl_seconds: int = Field(300, ge=60, le=900)

    homelab_poll_interval: int = Field(60, ge=10, le=86400)
    proactive_mode: bool = False
    cpu_alert_threshold: float = Field(90, ge=1, le=100)
    memory_alert_threshold: float = Field(90, ge=1, le=100)
    event_cooldown_seconds: int = Field(900, ge=30)
    memory_retention_days: int = Field(90, ge=1)
    memory_max_short_term: int = Field(40, ge=4, le=500)

    homelab_enabled: bool = True
    homelab_mutations_enabled: bool = False
    homelab_registry_path: Path = Field(
        default_factory=lambda: _private_config_path("homelab_hosts.local.yaml", "homelab_hosts.example.yaml")
    )
    homelab_default_timeout_seconds: float = Field(5, ge=1, le=30)
    homelab_overview_cache_seconds: float = Field(5, ge=0, le=60)
    homelab_offline_failure_threshold: int = Field(2, ge=1, le=10)

    proxmox_enabled: bool = True
    proxmox_url: str = ""
    proxmox_token_id: str = ""
    proxmox_token_secret: str = ""
    proxmox_verify_ssl: bool = True
    proxmox_tls_fingerprint: str = ""
    openwrt_url: str = ""
    openwrt_username: str = ""
    openwrt_password: str = ""

    home_assistant_enabled: bool = True
    home_assistant_url: str = ""
    home_assistant_token: str = ""

    @field_validator("ollama_url", "proxmox_url", "openwrt_url", "home_assistant_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("ollama_keep_alive")
    @classmethod
    def validate_ollama_keep_alive(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        if cleaned in {"-1", "0"}:
            return cleaned
        import re
        if not re.fullmatch(r"[1-9]\d*(?:s|m|h)", cleaned):
            raise ValueError("ollama_keep_alive must be -1, 0 or a positive duration such as 30m")
        return cleaned

    @field_validator("database_path")
    @classmethod
    def resolve_database_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        parts = value.parts[1:] if value.parts and value.parts[0].casefold() == "data" else value.parts
        return DATA_ROOT.joinpath(*parts)

    @field_validator(
        "chatterbox_python",
        "shell_default_working_directory",
        "trusted_hosts_path",
        "homelab_registry_path",
    )
    @classmethod
    def resolve_model_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("chatterbox_reference")
    @classmethod
    def resolve_private_audio_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        parts = value.parts[1:] if value.parts and value.parts[0].casefold() == "data" else value.parts
        return DATA_ROOT.joinpath(*parts)

    @field_validator("selfdev_workspace", "selfdev_canonical_root", "selfdev_public_snapshot")
    @classmethod
    def resolve_selfdev_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("tts_model_path", "tts_voices_path")
    @classmethod
    def resolve_tts_asset_path(cls, value: Path) -> Path:
        # Assets do Kokoro são somente-leitura: no instalado vêm do resource
        # embutido (backend-runtime/_internal), nunca de cwd/repo/.venv.
        if value.is_absolute():
            return value
        if value.parts and value.parts[0].casefold() == "data":
            local_asset = DATA_ROOT.joinpath(*value.parts[1:])
            if local_asset.exists():
                return local_asset
            packaged_asset = resolve_packaged_path(value)
            if packaged_asset.exists():
                return packaged_asset
            return local_asset
        return resolve_packaged_path(value)

    @model_validator(mode="after")
    def validate_shell_limits(self) -> "Settings":
        if self.shell_timeout_seconds > self.shell_max_timeout_seconds:
            raise ValueError("shell_timeout_seconds cannot exceed shell_max_timeout_seconds")
        if self.ssh_command_timeout_seconds > self.ssh_max_timeout_seconds:
            raise ValueError("ssh_command_timeout_seconds cannot exceed ssh_max_timeout_seconds")
        return self

    @classmethod
    def from_sources(cls, **overrides: Any) -> "Settings":
        values = _yaml_defaults()
        for state_path in (
            DATA_ROOT / "settings-adult.json",
            DATA_ROOT / "settings-v33.json",
        ):
            if not state_path.is_file():
                continue
            try:
                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                values.update(persisted)
            except (OSError, ValueError):
                pass
        # Existing releases retain their copied settings in LocalAppData. The
        # user-approved Ava identity supersedes both legacy Dora and the
        # rejected blended voice exactly once through an explicit version.
        if values.get("tts_voice_identity_version") != "ava-v1":
            values.update({
                "tts_provider": "edge_tts",
                "tts_voice": "en-US-AvaMultilingualNeural",
                "tts_voice_identity_version": "ava-v1",
            })
        values.update(overrides)
        # Deployment environment (.env/process) wins over persisted UI choices,
        # which win over YAML.  Removing those keys lets BaseSettings parse and
        # validate NYRA_* values instead of silently masking them with kwargs.
        env_names = {name.casefold() for name in os.environ}
        env_path = PROJECT_ROOT / ".env"
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                    match = re.match(r"\s*(?:export\s+)?(NYRA_[A-Za-z0-9_]+)\s*=", line)
                    if match:
                        env_names.add(match.group(1).casefold())
            except OSError:
                pass
        for key in tuple(values):
            if key not in overrides and f"NYRA_{key}".casefold() in env_names:
                values.pop(key, None)
        return cls(**values)

    def public_dict(self) -> dict[str, Any]:
        hidden = {
            "proxmox_token_id", "proxmox_token_secret",
            "openwrt_password", "sentinel_bridge_token",
            "home_assistant_token",
        }
        return {
            key: ("***configured***" if key in hidden and value else value)
            for key, value in self.model_dump(mode="json").items()
            if key != "sentinel_bridge_token" and (key not in hidden or value)
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_sources()
