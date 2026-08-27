#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
(cd "$ROOT/backend" && "$ROOT/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000) &
BACKEND_PID=$!
(cd "$ROOT/frontend" && npm run dev) &
FRONTEND_PID=$!
printf '{"backend":%s,"frontend":%s}\n' "$BACKEND_PID" "$FRONTEND_PID" > "$ROOT/.nyra-processes.json"
echo 'NYRA: http://127.0.0.1:5173'
