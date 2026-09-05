"""Targeted release-input policy; no freezer execution or operator data reads."""
from __future__ import annotations

import ast
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "packaging/nyra-backend.spec"


@pytest.fixture
def spec(monkeypatch, tmp_path):
    models = tmp_path / "data/models"
    models.mkdir(parents=True)
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        (models / name).touch()
    monkeypatch.setenv("NYRA_DATA_HOME", str(tmp_path))
    captured = {}

    def analysis(*args, **kwargs):
        captured["analysis"] = kwargs
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

    def exe(*args, **kwargs):
        captured["exe"] = kwargs
        return object()

    def collect(*args, **kwargs):
        captured["collect"] = kwargs

    namespace = runpy.run_path(str(SPEC), init_globals={
        "SPECPATH": str(SPEC.parent), "Analysis": analysis,
        "PYZ": lambda _: [], "EXE": exe, "COLLECT": collect,
    })
    return namespace, captured


def test_public_assets_are_explicit_and_no_directory_collection(spec):
    namespace, captured = spec
    rows = namespace["public_asset_toc"](REPO)
    assert len(rows) == len({row[0] for row in rows})
    assert all(Path(row[1]).is_file() for row in rows)
    assert not any(".local." in row[0] or ".env" in row[0] for row in rows)
    assert not any(Path(source).name in {"config", "identity"}
                   for source, _ in captured["analysis"]["datas"])


def test_installed_registries_come_from_empty_public_templates(spec):
    namespace, _ = spec
    sources = {name: Path(source) for name, source, _ in namespace["public_asset_toc"](REPO)}
    assert sources["config/homelab_hosts.yaml"] == REPO / "config/homelab_hosts.example.yaml"
    assert sources["config/network_aliases.json"] == REPO / "config/network_aliases.example.json"
    import json
    import yaml
    assert yaml.safe_load(sources["config/homelab_hosts.yaml"].read_text())['hosts'] == []
    assert json.loads(sources["config/network_aliases.json"].read_text())['hosts'] == []


def test_missing_public_asset_fails_closed(spec, tmp_path):
    namespace, _ = spec
    with pytest.raises(SystemExit, match="public release asset missing"):
        namespace["public_asset_toc"](tmp_path)


def test_voice_assets_and_production_capabilities_preserved(spec):
    _, captured = spec
    analysis = captured["analysis"]
    names = {Path(source).name for source, _ in analysis["datas"]}
    assert {"kokoro-v1.0.int8.onnx", "voices-v1.0.bin", "silero_vad_v6.onnx", "espeak-ng.dll"} <= names
    assert analysis["excludes"] == ["fsspec.conftest", "pytest", "_pytest"]
    assert analysis["runtime_hooks"] == []
    assert analysis["hookspath"] == []


def test_onedir_no_upx_no_obfuscation(spec):
    _, captured = spec
    assert captured["exe"]["exclude_binaries"] is True
    for target in ("exe", "collect"):
        assert captured[target]["upx"] is False
        assert captured[target]["strip"] is False
    assert captured["analysis"]["optimize"] == 0


def test_builder_uses_isolated_clean_cache_and_hashes_before_promotion_marker():
    source = (REPO / "packaging/build-backend.ps1").read_text(encoding="utf-8")
    assert "$env:PYINSTALLER_CONFIG_DIR = Join-Path $stageRoot 'cache'" in source
    assert "-m PyInstaller --clean --noconfirm" in source
    assert "$env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerConfig" in source
    assert source.index("Get-FileHash") < source.index("executable_sha256 = $hash")
    ast.parse(SPEC.read_text(encoding="utf-8"))
