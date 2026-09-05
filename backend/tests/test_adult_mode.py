from app.core.config import Settings


def test_adult_mode_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("KAZUMI_ADULT_MODE_ENABLED", "false")
    settings = Settings.from_sources()
    assert settings.adult_mode_enabled is False
