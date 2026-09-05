# -*- mode: python ; coding: utf-8 -*-

import importlib.util
import os
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
REPO = SPEC_DIR.parent


# Only public release assets. Never collect the config/identity trees wholesale:
# ignored operator files can exist beside tracked templates in the working tree.
PUBLIC_CONFIG_FILES = (
    "default.yaml", "desktop_apps.yaml", "homelab_hosts.example.yaml",
    "live2d_reactions.yaml", "network_aliases.example.json",
    "runtime_services.yaml", "skills/core.yaml", "vtube_parameter_mapping.yaml",
    "workflow_templates.json",
)
PUBLIC_IDENTITY_FILES = (
    "asset_guide.md", "image_generation_prompt.md", "live2d_art_spec.md",
    "lore.md", "personality.md", "pronunciation_ptbr.defaults.json",
    "pronunciation_ptbr.json", "system_prompt.md", "visual_bible.md",
    "visual_states.md", "voice_bible.md", "voice_profile.json",
)


def public_asset_toc(repo: Path) -> list[tuple[str, str, str]]:
    assets = [
        (f"{directory}/{name}", str(repo / directory / name), "DATA")
        for directory, names in (("config", PUBLIC_CONFIG_FILES), ("identity", PUBLIC_IDENTITY_FILES))
        for name in names
    ]
    # Installed first-run defaults must be empty public registries, not the
    # operator's actual host/alias files. Existing runtime configs are untouched.
    assets.extend([
        ("config/homelab_hosts.yaml", str(repo / "config/homelab_hosts.example.yaml"), "DATA"),
        ("config/network_aliases.json", str(repo / "config/network_aliases.example.json"), "DATA"),
    ])
    for _, source, _ in assets:
        path = Path(source)
        if not path.is_file() or not path.resolve().is_relative_to(repo.resolve()):
            raise SystemExit("public release asset missing or outside repository: " + path.name)
    return assets


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise SystemExit(f"pacote obrigatorio ausente no build: {name}")
    return Path(spec.origin).parent


def _runtime_root() -> Path:
    configured = os.environ.get("KAZUMI_DATA_HOME")
    if configured:
        return Path(configured).resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return (Path(local) / "KAZUMI").resolve()
    return (Path.home() / "AppData" / "Local" / "KAZUMI").resolve()


model_dir = _runtime_root() / "data" / "models"
model_files = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
missing = [name for name in model_files if not (model_dir / name).is_file()]
if missing:
    raise SystemExit(
        "modelos TTS ausentes no runtime: " + ", ".join(missing)
        + "; execute scripts/download_tts_models.ps1"
    )

espeak_dir = _package_dir("espeakng_loader")
kokoro_dir = _package_dir("kokoro_onnx")
faster_whisper_dir = _package_dir("faster_whisper")
tts_datas = [
    (str(model_dir / "kokoro-v1.0.int8.onnx"), "data/models"),
    (str(model_dir / "voices-v1.0.bin"), "data/models"),
    (str(espeak_dir / "espeak-ng.dll"), "espeakng_loader"),
    (str(espeak_dir / "espeak-ng-data"), "espeakng_loader/espeak-ng-data"),
    (str(kokoro_dir / "config.json"), "kokoro_onnx"),
]
stt_datas = [
    (str(faster_whisper_dir / "assets" / "silero_vad_v6.onnx"), "faster_whisper/assets"),
]

a = Analysis(
    [str(REPO / "backend" / "run_backend.py")],
    pathex=[str(REPO / "backend")],
    binaries=[],
    datas=[
        *tts_datas,
        *stt_datas,
    ],
    hiddenimports=["kokoro_onnx", "espeakng_loader", "phonemizer", "win32timezone"],
    # The upstream fsspec hook collects fsspec.conftest, pulling pytest into the
    # application. These are development tests, never backend runtime features.
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["fsspec.conftest", "pytest", "_pytest"],
    noarchive=False, optimize=0,
)
a.datas += public_asset_toc(REPO)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="kazumi-backend",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[],
    name="kazumi-backend",
)
