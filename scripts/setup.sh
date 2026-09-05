#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KAZUMI_RUNTIME_ROOT="${KAZUMI_DATA_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/KAZUMI}"
python3.11 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/backend/requirements-dev.txt"
(cd "$ROOT/frontend" && npm install)
[[ -f "$ROOT/.env" ]] || cp "$ROOT/.env.example" "$ROOT/.env"
mkdir -p "$KAZUMI_RUNTIME_ROOT/data/models"
[[ -s "$KAZUMI_RUNTIME_ROOT/data/models/kokoro-v1.0.int8.onnx" ]] || curl -fL -o "$KAZUMI_RUNTIME_ROOT/data/models/kokoro-v1.0.int8.onnx" https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx
[[ -s "$KAZUMI_RUNTIME_ROOT/data/models/voices-v1.0.bin" ]] || curl -fL -o "$KAZUMI_RUNTIME_ROOT/data/models/voices-v1.0.bin" https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
"$ROOT/.venv/bin/python" "$ROOT/scripts/preload_stt.py"
echo 'Setup concluído.'
