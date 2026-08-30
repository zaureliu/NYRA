# -*- mode: python ; coding: utf-8 -*-

import importlib.util
import os
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
REPO = SPEC_DIR.parent


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise SystemExit(f"pacote obrigatorio ausente no build: {name}")
    return Path(spec.origin).parent


def _runtime_root() -> Path:
    configured = os.environ.get("NYRA_DATA_HOME")
    if configured:
        return Path(configured).resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return (Path(local) / "NYRA").resolve()
    return (Path.home() / "AppData" / "Local" / "NYRA").resolve()


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
        (str(REPO / "config"), "config"),
        (str(REPO / "identity"), "identity"),
        *tts_datas,
        *stt_datas,
    ],
    hiddenimports=["kokoro_onnx", "espeakng_loader", "phonemizer", "win32timezone"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="nyra-backend",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=True, upx_exclude=[],
    name="nyra-backend",
)
