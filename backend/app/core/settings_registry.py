"""Settings Service V3 (prompt11 Parte I, §38-§43).

Fonte única de verdade para settings expostas à UI:

* Backend é a autoridade; o frontend nunca persiste settings localmente.
* Cada setting possui schema declarativo (categoria/tipo/default/sensitive/
  requires_restart/descrição/validação).
* Secrets NUNCA passam por aqui: são representadas como
  ``{"configured": true|false}`` e continuam no Credential Broker / secret
  stores dedicados (Sentinel, Home Assistant).
* Persistência não-secreta em ``data/settings-v33.json`` via runtime_settings,
  recarregada por ``Settings.from_sources`` no boot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.runtime_settings import save_runtime_settings


@dataclass(frozen=True)
class SettingSpec:
    key: str
    category: str  # general|ai|voice|desktop|automation|homelab|integrations|privacy|developer
    type: str      # bool|int|float|str|enum
    default: Any
    description: str = ""
    sensitive: bool = False
    requires_restart: bool = False
    options: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None


SENSITIVE_KEYS: dict[str, str] = {
    # key -> fluxo correto de configuração (Credential Broker / secret store)
    "sentinel_bridge_token": "PUT /api/sentinel-watch/token",
    "home_assistant_token": "POST /api/home-assistant/profiles/{id}/token",
    "proxmox_token_secret": "config/homelab_hosts.yaml + Credential Broker",
    "proxmox_token_id": "config/homelab_hosts.yaml + Credential Broker",
    "openwrt_password": "config/homelab_hosts.yaml + Credential Broker",
}

ENTRIES: tuple[SettingSpec, ...] = (
    # ---------------------------------------------------------------- general
    SettingSpec("adult_mode_enabled", "general", "bool", False,
                "Habilita conteúdo adulto (+18) com confirmação explícita."),
    SettingSpec("desktop_start_with_windows", "general", "bool", False,
                "Iniciar presença desktop junto com o Windows.", requires_restart=True),
    SettingSpec("microphone", "general", "str", "default",
                "Dispositivo de microfone preferido."),
    SettingSpec("speaker", "general", "str", "default",
                "Dispositivo de saída de áudio preferido."),
    # --------------------------------------------------------------------- ai
    SettingSpec("agent_max_steps", "ai", "int", 12, "Passos máximos por Agent Run.",
                minimum=1, maximum=50),
    SettingSpec("agent_max_tool_calls", "ai", "int", 20,
                "Tool calls máximas por Agent Run.", minimum=1, maximum=100),
    SettingSpec("agent_max_runtime_seconds", "ai", "int", 300,
                "Tempo máximo de um Agent Run (segundos).", minimum=10, maximum=3600),
    SettingSpec("agent_read_only", "ai", "bool", False,
                "Agent Loop somente leitura (bloqueia risco mutável)."),
    SettingSpec("agent_max_identical_repeats", "ai", "int", 2,
                "Detecção de repetição: repetições idênticas toleradas.",
                minimum=1, maximum=10),
    SettingSpec("turn_isolation_enabled", "ai", "bool", True,
                "Isolamento estrito entre turnos de conversa.", requires_restart=True),
    SettingSpec("conversation_single_active_turn", "ai", "bool", True,
                "Um único turno ativo por vez.", requires_restart=True),
    # ------------------------------------------------------------------ voice
    SettingSpec("audio_volume", "voice", "float", 0.9, "Volume da fala sintetizada.",
                minimum=0.0, maximum=1.0),
    SettingSpec("mic_gain", "voice", "float", 1.0, "Ganho do microfone.", minimum=0.25, maximum=4.0),
    SettingSpec("vad_enabled", "voice", "bool", True, "VAD Silero local no STT."),
    SettingSpec("vad_threshold", "voice", "float", 0.5, "Limiar do VAD.", minimum=0.0, maximum=1.0),
    SettingSpec("voice_barge_in", "voice", "bool", True,
                "Permitir interrupção da NYRA pela voz do operador."),
    SettingSpec("voice_stream_tts", "voice", "bool", True, "Streaming de TTS por sentenças."),
    SettingSpec("listening_mode", "voice", "enum", "hands_free",
                "Modo de conversação por voz.",
                options=("push_to_talk", "wake_word", "hands_free")),
    SettingSpec("wake_word", "voice", "str", "Nyra", "Palavra de desperta."),
    SettingSpec("hands_free_timeout_seconds", "voice", "int", 120,
                "Timeout da sessão hands-free (segundos).", minimum=15, maximum=3600),
    SettingSpec("always_listening_enabled", "voice", "bool", True,
                "Escuta contínua habilitada (toggle também na página Voice)."),
    # ---------------------------------------------------------------- desktop
    SettingSpec("desktop_always_on_top", "desktop", "bool", True,
                "Presença desktop sempre acima das janelas."),
    SettingSpec("desktop_click_through", "desktop", "bool", False,
                "Clicar através da presença desktop."),
    SettingSpec("desktop_overlay_scale", "desktop", "float", 1.0,
                "Escala da presença desktop.", minimum=0.5, maximum=2.0),
    SettingSpec("desktop_speech_bubble", "desktop", "bool", True,
                "Balão de fala na presença desktop."),
    SettingSpec("desktop_idle_animation", "desktop", "bool", True,
                "Animação idle na presença desktop."),
    SettingSpec("desktop_dynamic_app_discovery", "desktop", "bool", True,
                "Descoberta dinâmica de aplicativos desktop."),
    # ------------------------------------------------------------- automation
    SettingSpec("shell_timeout_seconds", "automation", "int", 30,
                "Timeout padrão do shell local (segundos).", minimum=1, maximum=300),
    SettingSpec("shell_confirm_destructive", "automation", "bool", True,
                "Exigir approval para comandos DESTRUCTIVE/CRITICAL."),
    SettingSpec("shell_approval_ttl_seconds", "automation", "int", 300,
                "Validade de um approval de uso único (segundos).", minimum=30, maximum=3600),
    SettingSpec("ssh_command_timeout_seconds", "automation", "int", 30,
                "Timeout de comando remoto SSH (segundos).", minimum=1, maximum=300),
    SettingSpec("elevated_session_default_ttl_seconds", "automation", "int", 300,
                "TTL padrão de sessão elevada (segundos).", minimum=60, maximum=900),
    # ---------------------------------------------------------------- homelab
    SettingSpec("homelab_poll_interval", "homelab", "int", 60,
                "Intervalo de poll do homelab (segundos).", minimum=10, maximum=86400),
    SettingSpec("homelab_mutations_enabled", "homelab", "bool", False,
                "Opt-in global para mutações de homelab; cada ação ainda exige approval."),
    SettingSpec("homelab_default_timeout_seconds", "homelab", "float", 5.0,
                "Timeout padrão de probes homelab (segundos).", minimum=1.0, maximum=30.0),
    SettingSpec("cpu_alert_threshold", "homelab", "float", 90.0,
                "Alerta de CPU (%) nos hosts.", minimum=1.0, maximum=100.0),
    SettingSpec("memory_alert_threshold", "homelab", "float", 90.0,
                "Alerta de memória (%) nos hosts.", minimum=1.0, maximum=100.0),
    SettingSpec("proxmox_verify_ssl", "homelab", "bool", True,
                "Verificar certificado TLS do Proxmox."),
    SettingSpec("proxmox_url", "homelab", "str", "", "URL base do Proxmox."),
    SettingSpec("openwrt_url", "homelab", "str", "", "URL base do OpenWrt (LuCI/RPC)."),
    SettingSpec("openwrt_username", "homelab", "str", "", "Usuário SSH/OpenWrt."),
    # ------------------------------------------------------------ integrations
    SettingSpec("network_voice_alerts", "integrations", "bool", True,
                "Alertas de rede por voz."),
    SettingSpec("network_desktop_alerts", "integrations", "bool", True,
                "Alertas de rede no desktop presence."),
    SettingSpec("sentinel_voice_alerts", "integrations", "bool", True,
                "Alertas do Sentinel por voz."),
    SettingSpec("sentinel_critical_only", "integrations", "bool", False,
                "Falar apenas alertas críticos do Sentinel."),
    SettingSpec("sentinel_auto_reconnect", "integrations", "bool", True,
                "Reconexão automática do bridge Sentinel com backoff."),
    SettingSpec("sentinel_event_retention_days", "integrations", "int", 30,
                "Retenção do histórico Sentinel (dias).", minimum=1, maximum=365),
    SettingSpec("sentinel_alert_cooldown_seconds", "integrations", "int", 300,
                "Cooldown entre alertas falados do Sentinel (segundos).",
                minimum=10, maximum=86400),
    SettingSpec("home_assistant_url", "integrations", "str", "",
                "URL do profile Home Assistant ativo."),
    # ---------------------------------------------------------------- privacy
    SettingSpec("listening_privacy_indicator", "privacy", "bool", True,
                "Indicador visual durante escuta contínua."),
    SettingSpec("listening_audio_debug", "privacy", "bool", False,
                "Guardar áudio bruto de debug (NÃO recomendado)."),
    SettingSpec("keep_debug_audio", "privacy", "bool", False,
                "Manter áudios de depuração em data/audio."),
    SettingSpec("network_quiet_mode", "privacy", "bool", False,
                "Suprimir alertas não críticos de rede."),
    # -------------------------------------------------------------- developer
    SettingSpec("sentinel_debug_mode", "developer", "bool", False,
                "Habilita injeção de eventos fake do Sentinel (debug)."),
    SettingSpec("pronunciation_debug", "developer", "bool", False,
                "Logs detalhados do engine de pronúncia."),
    SettingSpec("shell_max_output_chars", "developer", "int", 50000,
                "Teto de captura de output do shell (caracteres).",
                minimum=1000, maximum=1000000),
    SettingSpec("runtime_health_interval_seconds", "developer", "int", 15,
                "Intervalo de health check dos serviços gerenciados (segundos).",
                minimum=5, maximum=600),
    # --------------------------------------------------------------- selfdev
    SettingSpec("selfdev_mode", "selfdev", "enum", "AUTONOMOUS_SAFE",
                "Modo do Self-Development Engine.",
                options=("OFF", "OBSERVE_ONLY", "AUTONOMOUS_SAFE", "AUTONOMOUS_ADVANCED")),
    SettingSpec("selfdev_model", "selfdev", "str", "qwen3:8b",
                "Modelo local instalado no Ollama usado somente pelo SelfDev."),
    SettingSpec("selfdev_workspace", "selfdev", "str", "../nyra-selfdev",
                "Workspace isolado de candidates e worktrees.", requires_restart=True),
    SettingSpec("selfdev_run_when_idle", "selfdev", "bool", True,
                "Executar candidates somente quando o sistema estiver ocioso."),
    SettingSpec("selfdev_auto_publish_github", "selfdev", "bool", False,
                "Publicar melhorias LOW_RISK após todos os gates. OFF no bootstrap 0.3.0."),
    SettingSpec("selfdev_max_auto_promotions_per_day", "selfdev", "int", 3,
                "Teto diário de promoções automáticas.", minimum=0, maximum=20),
    SettingSpec("selfdev_max_candidate_runtime_minutes", "selfdev", "int", 30,
                "Timeout total de um candidate em minutos.", minimum=1, maximum=240),
    SettingSpec("selfdev_max_files_low_risk", "selfdev", "int", 8,
                "Máximo de arquivos em candidate LOW_RISK.", minimum=1, maximum=100),
    SettingSpec("selfdev_max_diff_lines_low_risk", "selfdev", "int", 500,
                "Máximo de linhas de diff em candidate LOW_RISK.", minimum=1, maximum=20000),
    SettingSpec("selfdev_cooldown_minutes", "selfdev", "int", 15,
                "Cooldown após candidate rejeitado ou bloqueado.", minimum=0, maximum=1440),
    # ------------------------------------------------- secrets (sempre mascarados)
    # Exibidos apenas como {"configured": true|false}; nunca aceitam valor pela
    # UI de settings (§42-§43). O fluxo correto é indicado em `configure_via`.
    SettingSpec("sentinel_bridge_token", "integrations", "str", "",
                "Token do bridge UTAMO Sentinel."),
    SettingSpec("home_assistant_token", "integrations", "str", "",
                "Token long-lived do profile Home Assistant ativo."),
    SettingSpec("proxmox_token_id", "homelab", "str", "",
                "ID do API token Proxmox."),
    SettingSpec("proxmox_token_secret", "homelab", "str", "",
                "Secret do API token Proxmox."),
    SettingSpec("openwrt_password", "homelab", "str", "",
                "Senha SSH do OpenWrt."),
)

# Índices derivados.
_BY_KEY = {entry.key: entry for entry in ENTRIES}

# Chaves cujo valor atual só pode vir de env/yaml e que exigem restart quando
# mudadas por arquivo — expostas como metadado honesto na UI.
RESTART_KEYS = {entry.key for entry in ENTRIES if entry.requires_restart}


class SettingValidationError(ValueError):
    """Erro de validação com código estável para a UI."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _configured_value(settings: Any, key: str) -> bool:
    return bool(str(getattr(settings, key, "") or "").strip())


def describe_entries() -> list[dict[str, Any]]:
    return [
        {
            "key": e.key,
            "category": e.category,
            "type": e.type,
            "default": e.default,
            "sensitive": e.key in SENSITIVE_KEYS,
            "requires_restart": e.requires_restart,
            "description": e.description,
            "options": list(e.options) if e.options else None,
            "minimum": e.minimum,
            "maximum": e.maximum,
        }
        for e in ENTRIES
    ]


def get_settings_v3(settings: Any) -> dict[str, Any]:
    items = []
    for e in ENTRIES:
        if e.key in SENSITIVE_KEYS:
            value: dict[str, Any] | Any = {"configured": _configured_value(settings, e.key)}
        else:
            value = getattr(settings, e.key, e.default)
            if isinstance(value, Path):
                value = str(value)
        item = {
            "key": e.key,
            "category": e.category,
            "type": "secret" if e.key in SENSITIVE_KEYS else e.type,
            "current": value,
            "default": e.default,
            "sensitive": e.key in SENSITIVE_KEYS,
            "requires_restart": e.requires_restart,
            "description": e.description,
            "options": list(e.options) if e.options else None,
            "minimum": e.minimum,
            "maximum": e.maximum,
        }
        if e.key in SENSITIVE_KEYS:
            item["configure_via"] = SENSITIVE_KEYS[e.key]
        items.append(item)
    return {
        "settings": items,
        "categories": sorted({e.category for e in ENTRIES}),
        "version": 3,
    }


def update_setting(settings: Any, key: str, value: Any) -> dict[str, Any]:
    entry = _BY_KEY.get(key)
    if entry is None:
        raise KeyError(key)
    if key in SENSITIVE_KEYS:
        raise PermissionError(f"'{key}' é segredo; configure via {SENSITIVE_KEYS[key]}")

    if entry.type == "bool":
        if not isinstance(value, bool):
            raise SettingValidationError("SETTINGS_INVALID_TYPE", "valor booleano esperado")
    elif entry.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingValidationError("SETTINGS_INVALID_TYPE", "valor inteiro esperado")
        if entry.minimum is not None and value < entry.minimum:
            raise SettingValidationError("SETTINGS_OUT_OF_RANGE", f"mínimo {entry.minimum}")
        if entry.maximum is not None and value > entry.maximum:
            raise SettingValidationError("SETTINGS_OUT_OF_RANGE", f"máximo {entry.maximum}")
    elif entry.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingValidationError("SETTINGS_INVALID_TYPE", "valor numérico esperado")
        value = float(value)
        if entry.minimum is not None and value < entry.minimum:
            raise SettingValidationError("SETTINGS_OUT_OF_RANGE", f"mínimo {entry.minimum}")
        if entry.maximum is not None and value > entry.maximum:
            raise SettingValidationError("SETTINGS_OUT_OF_RANGE", f"máximo {entry.maximum}")
    elif entry.type == "enum":
        if not isinstance(value, str) or value not in (entry.options or ()):
            raise SettingValidationError(
                "SETTINGS_INVALID_OPTION", f"opções válidas: {', '.join(entry.options or ())}"
            )
    else:  # str
        if not isinstance(value, str):
            raise SettingValidationError("SETTINGS_INVALID_TYPE", "texto esperado")
        if len(value) > 500:
            raise SettingValidationError("SETTINGS_TOO_LONG", "texto muito longo (máx 500)")

    setattr(settings, key, value)
    save_runtime_settings({key: value})
    return {
        "key": key,
        "current": value,
        "requires_restart": entry.requires_restart,
        "persisted": True,
    }


def export_config(settings: Any, about: dict[str, Any]) -> dict[str, Any]:
    """Export seguro (prompt11 Parte BE §235-§236): nada secreto sai daqui."""
    entries = []
    for e in ENTRIES:
        if e.key in SENSITIVE_KEYS:
            value = {"configured": _configured_value(settings, e.key)}
        else:
            value = getattr(settings, e.key, e.default)
            if isinstance(value, Path):
                value = str(value)
        entries.append({"key": e.key, "category": e.category,
                        "type": e.type, "value": value})
    return {
        "exported_at": about.get("generated_at"),
        "nyra_version": about.get("version"),
        "settings": entries,
        "note": "Segredos aparecem apenas como {\"configured\": true|false}.",
    }
