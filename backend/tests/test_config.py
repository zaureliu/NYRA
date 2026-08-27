from pathlib import Path

from app.core import config
from app.core.config import Settings


def test_settings_resolve_relative_database_path():
    settings = Settings.from_sources(database_path=Path("data/test.db"), backend_port=8123)
    assert settings.database_path.is_absolute()
    assert settings.database_path.name == "test.db"
    assert settings.backend_port == 8123
    assert settings.llm_model == "qwen3:8b"
    assert settings.shell_enabled is True
    assert settings.shell_default == "powershell"
    assert settings.shell_timeout_seconds == 30
    assert settings.shell_max_timeout_seconds == 300
    assert settings.remote_shell_enabled is True
    assert settings.ssh_connect_timeout_seconds == 5
    assert settings.ssh_command_timeout_seconds == 30
    assert settings.agent_enabled is True
    assert settings.agent_max_steps == 12
    assert settings.agent_max_tool_calls == 20
    assert settings.agent_max_runtime_seconds == 300


def test_secrets_are_masked():
    settings = Settings.from_sources(proxmox_token_secret="super-secret")
    public = settings.public_dict()
    assert public["proxmox_token_secret"] == "***configured***"
    assert "super-secret" not in str(public)


def test_hands_on_is_the_fresh_install_default(tmp_path):
    settings = Settings(database_path=tmp_path / "nyra.db")
    assert settings.always_listening_enabled is True
    assert settings.listening_mode == "hands_free"


def test_tts_asset_prefers_existing_local_override_then_packaged_asset(tmp_path, monkeypatch):
    data_root = tmp_path / "runtime" / "data"
    packaged = tmp_path / "bundle" / "data" / "models" / "kokoro.onnx"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"packaged")
    monkeypatch.setattr(config, "DATA_ROOT", data_root)
    monkeypatch.setattr(config, "resolve_packaged_path", lambda value: tmp_path / "bundle" / value)

    relative = Path("data/models/kokoro.onnx")
    assert Settings.resolve_tts_asset_path(relative) == packaged

    local = data_root / "models" / "kokoro.onnx"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"operator override")
    assert Settings.resolve_tts_asset_path(relative) == local
