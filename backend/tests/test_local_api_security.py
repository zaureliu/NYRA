from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
import pytest

from app.core.local_transport import LocalRequestSecurityMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LocalRequestSecurityMiddleware, frontend_port=5173, backend_port=8000)

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_json({"ok": True})

    return app


def test_rejects_host_origin_and_cross_site_fetch(monkeypatch):
    monkeypatch.setenv("KAZUMI_TESTING", "true")
    with TestClient(_app()) as client:
        assert client.post("/mutate", headers={"host": "evil.example:8000"}).status_code == 403
        assert client.post("/mutate", headers={"origin": "http://evil.example"}).status_code == 403
        assert client.post("/mutate", headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_allows_native_no_origin_and_trusted_ui(monkeypatch):
    monkeypatch.setenv("KAZUMI_TESTING", "true")
    with TestClient(_app()) as client:
        assert client.post("/mutate").json() == {"ok": True}
        response = client.post("/mutate", headers={"origin": "http://127.0.0.1:5173"})
        assert response.status_code == 200


def test_websocket_rejects_hostile_origin(monkeypatch):
    monkeypatch.setenv("KAZUMI_TESTING", "true")
    with TestClient(_app()) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
                pass
        with client.websocket_connect("/ws", headers={"origin": "tauri://localhost"}) as socket:
            assert socket.receive_json() == {"ok": True}
