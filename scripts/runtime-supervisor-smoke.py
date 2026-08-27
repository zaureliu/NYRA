"""Smoke real do Runtime Supervisor V1 via app FastAPI completa (hooks reais).

Fases:
1) Read-only: estados reais de todos os serviços registrados.
2) ACT -> VERIFY real e reversível em nyra_test_service (start -> health -> restart -> health -> stop).
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import os

os.environ.setdefault("NYRA_OLLAMA_PRELOAD", "false")
os.environ.setdefault("NYRA_CONVERSATION_ENGINE", "false")

from fastapi.testclient import TestClient

from app.main import app


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=1)[:1600])


def main() -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        tools = client.get("/api/tools").json()
        runtime_tools = sorted(item["name"] for item in tools if item["name"].startswith("runtime_"))
        print("runtime tools registradas:", runtime_tools)
        assert {"runtime_status", "runtime_start", "runtime_stop", "runtime_restart", "runtime_health", "runtime_logs"} <= set(runtime_tools)

        listing = client.get("/api/runtime/services")
        assert listing.status_code == 200
        services = {item["id"]: item for item in listing.json()["services"]}
        show("SERVIÇOS (read-only)", listing.json())
        for expected in ("nyra_backend", "nyra_frontend_dev", "ollama", "utamo_sentinel", "nyra_test_service"):
            assert expected in services, expected

        for service_id in services:
            detail = client.get(f"/api/runtime/services/{service_id}")
            assert detail.status_code == 200, (service_id, detail.status_code, detail.text[:200])
            health = client.get(f"/api/runtime/services/{service_id}/health")
            assert health.status_code == 200, (service_id, health.status_code, health.text[:300])
            logs = client.get(f"/api/runtime/services/{service_id}/logs")
            assert logs.status_code == 200, (service_id, logs.status_code)
        missing = client.get("/api/runtime/services/inexistente")
        assert missing.status_code == 404

        # ACT -> VERIFY real e reversível no serviço de teste seguro
        started = client.post("/api/runtime/services/nyra_test_service/start", json={}).json()
        show("START nyra_test_service", started)
        assert started["success"] is True and started["effect_verified"] is True and started["state"] == "READY"

        health = client.get("/api/runtime/services/nyra_test_service/health").json()
        show("HEALTH nyra_test_service", health)
        assert health["health"]["healthy"] is True

        restarted = client.post("/api/runtime/services/nyra_test_service/restart", json={}).json()
        show("RESTART nyra_test_service", restarted)
        assert restarted["success"] is True and restarted["state"] == "READY"

        logs = client.get("/api/runtime/services/nyra_test_service/logs?lines=20").json()
        show("LOGS nyra_test_service", logs)
        assert logs["exists"] is True and any("online" in line for line in logs["lines"])

        stopped = client.post("/api/runtime/services/nyra_test_service/stop", json={}).json()
        show("STOP nyra_test_service", stopped)
        assert stopped["success"] is True and stopped["state"] == "STOPPED"

        history = client.get("/api/runtime/history").json()
        show("HISTÓRICO", history)
        actions = [event["action"] for event in history["events"][:4]]
        assert {"start", "restart", "stop"} <= set(actions)

        self_restart = client.post("/api/runtime/services/nyra_backend/restart", json={}).json()
        show("SELF-RESTART bloqueado (limitação declarada)", self_restart)
        assert self_restart["error_code"] == "SELF_RESTART_UNSUPPORTED"

    print("\nSMOKE RUNTIME SUPERVISOR: OK")


if __name__ == "__main__":
    main()
