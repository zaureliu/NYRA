"""Selector real de modelo Ollama (ConfiguraÃ§Ãµes â†’ IA).

Cobre: discovery SEM lista hardcoded (todos os modelos de /api/tags com
metadata honesta), validaÃ§Ã£o contra o Ollama REAL, load com warm-up isolado
+ residency /api/ps, estados MODEL_*, concorrÃªncia e turno ativo.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes as api_routes
from app.brain.manager import BrainManager
from app.core.config import Settings


TAGS_PAYLOAD = {
    "models": [
        {
            "name": "qwen3:8b",
            "model": "qwen3:8b",
            "size": 5_200_000_000,
            "modified_at": "2026-08-01T12:00:00Z",
            "digest": "a" * 12 + "rest",
            "details": {"family": "qwen2", "parameter_size": "8B",
                        "quantization_level": "Q4_K_M"},
        },
        {
            "name": "llama3.2:3b",
            "size": 2_000_000_000,
            "modified_at": "2026-07-20T09:00:00Z",
            "digest": "b" * 12 + "rest",
            "details": {"family": "llama", "parameter_size": "3B",
                        "quantization_level": "Q4_K_M"},
        },
    ],
}

# Referência estável ao client real: chamar _patch_tags duas vezes no mesmo
# teste não deve aninhar factories (factory capturando factory patcheada).
REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_tags(
    monkeypatch,
    tags=TAGS_PAYLOAD,
    fail=False,
    running=({"name": "llama3.2:3b"},),
    malformed_tags=False,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail or request.url.path == "/api/boom":
            raise httpx.ConnectError("down")
        if request.url.path == "/api/tags":
            if malformed_tags:
                return httpx.Response(200, text="not-json")
            return httpx.Response(200, json=tags)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": list(running)})
        raise AssertionError(f"unexpected path {request.url.path}")

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# Discovery: todos os modelos reais, sem hardcode
# ---------------------------------------------------------------------------

async def test_inventory_lists_all_real_models_with_metadata(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    inventory = await brain.inventory()
    names = [item["name"] for item in inventory["models"]]
    assert names == ["qwen3:8b", "llama3.2:3b"]  # NÃƒO filtrado por whitelist
    llama = inventory["models"][1]
    for key in ("size", "modified_at", "digest", "family",
                "parameter_size", "quantization_level"):
        assert key in llama
    assert llama["family"] == "llama"
    assert llama["loaded"] is True  # presente em /api/ps
    qwen = inventory["models"][0]
    assert qwen["loaded"] is False
    assert qwen["quantization_level"] == "Q4_K_M"
    assert inventory["ollama_ready"] is True
    assert inventory["active_model"] == "llama3.2:3b"
    assert inventory["residency_known"] is True


async def test_inventory_reports_offline_without_inventing_models(monkeypatch):
    _patch_tags(monkeypatch, fail=True)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    inventory = await brain.inventory()
    assert inventory["ollama_ready"] is False
    assert inventory["ollama_state"] == "OFFLINE"
    assert inventory["active_model"] is None
    assert inventory["models"] == []


async def test_inventory_does_not_call_schema_error_offline(monkeypatch):
    _patch_tags(monkeypatch, malformed_tags=True)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    inventory = await brain.inventory()
    assert inventory["ollama_ready"] is True  # /api/ps respondeu de verdade
    assert inventory["ollama_state"] == "READY"
    assert inventory["inventory_error_code"] == "OLLAMA_SCHEMA_ERROR"
    assert inventory["active_model"] == "llama3.2:3b"


async def test_is_installed_validates_against_live_ollama(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    assert await brain.is_installed("qwen3:8b") is True
    assert await brain.is_installed("mistral:7b") is False


async def test_configured_model_not_installed_flag(monkeypatch, tmp_path):
    from app.brain import manager as manager_mod

    monkeypatch.setattr(manager_mod, "SETTINGS_PATH", tmp_path / "brain-settings.json")
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "phi4:14b")
    inventory = await brain.inventory()
    assert inventory["configured_model_not_installed"] is True


# ---------------------------------------------------------------------------
# Estados Â§4 via GET /brain/status
# ---------------------------------------------------------------------------

def _build_app(brain, warm=None, conversation_state="IDLE") -> FastAPI:
    app = FastAPI()
    app.include_router(api_routes.router)
    services = SimpleNamespace(
        settings=Settings(llm_model="qwen3:8b"),
        brain=brain,
        warm_manager=warm,
        conversation=SimpleNamespace(state=conversation_state),
    )
    app.state.services = services
    return app


class StubWarm:
    def __init__(self, state="OLLAMA_READY", ready=True, model="qwen3:8b"):
        self._payload = {"state": state, "ready": ready, "model": model,
                         "last_error": None, "metrics": {"resident": ready}}
        self.preload_calls = 0

    async def preload(self, *, force: bool = False) -> dict:
        self.preload_calls += 1
        return dict(self._payload)

    async def request_rewarm(self) -> None:
        return None

    def status(self) -> dict:
        return dict(self._payload)


async def test_status_ready_when_warm_and_resident(monkeypatch):
    _patch_tags(monkeypatch)

    class ResidentBrain(BrainManager):
        async def ready(self):
            return True

    app = _build_app(ResidentBrain("http://127.0.0.1:11434", "qwen3:8b"),
                     warm=StubWarm(state="OLLAMA_READY", ready=True))
    with TestClient(app) as client:
        payload = client.get("/api/brain/status").json()
    assert payload["state"] == "MODEL_READY"


async def test_status_offline_and_no_models(monkeypatch):
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")

    _patch_tags(monkeypatch, fail=True)
    app = _build_app(brain, warm=StubWarm())
    with TestClient(app) as client:
        assert client.get("/api/brain/status").json()["state"] == "OLLAMA_OFFLINE"

    _patch_tags(monkeypatch, tags={"models": []})
    app2 = _build_app(brain, warm=StubWarm())
    with TestClient(app2) as client:
        payload = client.get("/api/brain/status").json()
    assert payload["state"] == "NO_MODELS_INSTALLED"


async def test_status_failed_when_warm_error(monkeypatch):
    _patch_tags(monkeypatch, running=[])
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    app = _build_app(brain, warm=StubWarm(state="OLLAMA_ERROR", ready=False))
    with TestClient(app) as client:
        assert client.get("/api/brain/status").json()["state"] == "MODEL_FAILED"


async def test_status_prefers_real_residency_over_stale_warm_error(monkeypatch):
    _patch_tags(monkeypatch, running=[{"name": "llama3.2:3b"}])
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    app = _build_app(brain, warm=StubWarm(state="OLLAMA_ERROR", ready=False))
    with TestClient(app) as client:
        payload = client.get("/api/brain/status").json()
    assert payload["state"] == "MODEL_READY"
    assert payload["ollama_ready"] is True
    assert payload["active_model"] == "llama3.2:3b"


async def test_status_ready_without_resident_model_keeps_active_empty(monkeypatch):
    _patch_tags(monkeypatch, running=[])
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    app = _build_app(brain, warm=StubWarm(state="OLLAMA_READY", ready=True))
    with TestClient(app) as client:
        payload = client.get("/api/brain/status").json()
    assert payload["state"] == "MODEL_AVAILABLE"
    assert payload["ollama_ready"] is True
    assert payload["active_model"] is None


# ---------------------------------------------------------------------------
# Load Â§8/Â§10 + erros normalizados Â§15
# ---------------------------------------------------------------------------

async def test_load_rejects_model_not_installed(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    app = _build_app(brain, warm=StubWarm())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/brain/model/load", json={"model": "mistral:7b"})
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["error_code"] == "MODEL_NOT_INSTALLED"


async def test_load_blocks_while_turn_active(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    app = _build_app(brain, warm=StubWarm(), conversation_state="SPEAKING")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/brain/model/load", json={"model": "llama3.2:3b"})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "TURN_ACTIVE"


async def test_load_full_chain_updates_runtime_and_confirms_residency(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")

    class ResidentBrain(BrainManager):
        async def ready(self):
            return True

    brain = ResidentBrain("http://127.0.0.1:11434", "qwen3:8b")
    warm = StubWarm(model="llama3.2:3b")
    app = _build_app(brain, warm=warm)
    with TestClient(app) as client:
        payload = client.post("/api/brain/model/load", json={"model": "llama3.2:3b"}).json()
    assert payload["state"] == "MODEL_READY"
    assert payload["active_model"] == "llama3.2:3b"
    assert brain.active_model == "llama3.2:3b"   # prÃ³ximo chat usa o novo modelo
    assert warm.preload_calls == 1               # passou pelo Warm Manager real


async def test_load_reports_not_resident_as_502(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    warm = StubWarm(state="OLLAMA_ERROR", ready=False, model="llama3.2:3b")
    app = _build_app(brain, warm=warm)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/brain/model/load", json={"model": "llama3.2:3b"})
    assert response.status_code == 502
    assert response.json()["detail"]["error_code"] == "MODEL_NOT_RESIDENT"


async def test_select_persists_official_and_rejects_uninstalled(monkeypatch, tmp_path):
    _patch_tags(monkeypatch)
    from app.brain import manager as manager_mod

    monkeypatch.setattr(manager_mod, "SETTINGS_PATH", tmp_path / "brain-settings.json")
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")
    app = _build_app(brain, warm=StubWarm())

    with TestClient(app, raise_server_exceptions=False) as client:
        bad = client.post("/api/brain/select", json={"model": "ghost:latest", "confirmed": True})
        assert bad.status_code == 422
        ok = client.post("/api/brain/select", json={"model": "llama3.2:3b", "confirmed": True})
        assert ok.status_code == 200
    assert brain.official_model == "llama3.2:3b"
    assert (tmp_path / "brain-settings.json").is_file()


async def test_reset_default_requires_default_installed(monkeypatch, tmp_path):
    from app.brain import manager as manager_mod

    monkeypatch.setattr(manager_mod, "SETTINGS_PATH", tmp_path / "brain-settings.json")
    brain = BrainManager("http://127.0.0.1:11434", "llama3.2:3b")

    # default ausente â‡’ DEFAULT_MODEL_NOT_INSTALLED, sem inventar sucesso
    _patch_tags(monkeypatch, tags={"models": [TAGS_PAYLOAD["models"][1]]})
    app = _build_app(brain, warm=StubWarm())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/brain/reset-default")
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "DEFAULT_MODEL_NOT_INSTALLED"

    # default presente â‡’ salvar + carregar + confirmar
    _patch_tags(monkeypatch)
    warm = StubWarm(model="qwen3:8b")

    class ResidentBrain(BrainManager):
        async def ready(self):
            return True

    resident = ResidentBrain("http://127.0.0.1:11434", "llama3.2:3b")
    app2 = _build_app(resident, warm=warm)
    with TestClient(app2) as client:
        payload = client.post("/api/brain/reset-default").json()
    assert payload["state"] == "MODEL_READY"
    assert payload["active_model"] == "qwen3:8b"
    assert resident.official_model == "qwen3:8b"


async def test_concurrent_loads_are_serialized_with_clear_error(monkeypatch):
    _patch_tags(monkeypatch)
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")

    class SlowWarm:
        def __init__(self):
            self.release = asyncio.Event()

        async def preload(self, *, force=False):
            await self.release.wait()
            return {"state": "OLLAMA_READY", "ready": True, "model": "llama3.2:3b"}

        def status(self):
            return {"state": "OLLAMA_READY", "ready": True}

    class ResidentBrain(BrainManager):
        async def ready(self):
            return True

    app = _build_app(ResidentBrain("http://127.0.0.1:11434", "qwen3:8b"),
                     warm=SlowWarm())
    lock = asyncio.Lock()
    app.state.model_op_lock = lock

    results: dict = {}

    async def first():
        await lock.acquire()
        try:
            # simula a operaÃ§Ã£o em voo segurando o lock do endpoint
            results["held"] = True
            await asyncio.sleep(0.05)
        finally:
            lock.release()

    async def second():
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/brain/model/load", json={"model": "llama3.2:3b"})
            results["status"] = response.status_code
            if response.status_code != 200:
                results["code"] = response.json()["detail"]["error_code"]

    await asyncio.gather(first(), second())
    assert results["status"] == 409
    assert results["code"] == "MODEL_CHANGE_IN_PROGRESS"
