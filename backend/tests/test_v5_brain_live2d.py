from __future__ import annotations
import asyncio
from pathlib import Path

import pytest

from app.avatar.controller import AvatarState
from app.avatar.vtube_studio.auth import VTSAuth
from app.avatar.vtube_studio.models import VTSConnectionState, VTubeStudioConfig
from app.avatar.vtube_studio.parameters import discover_ids, parameter_values, resolve_mapping
from app.avatar.vtube_studio.protocol import request
from app.avatar.vtube_studio.provider import VTubeStudioAvatarProvider
from app.brain.manager import BrainManager
from app.llm.ollama import OllamaProvider


def test_vts_protocol_and_parameter_discovery_do_not_invent_ids():
    message=request("APIStateRequest")
    assert message["apiName"]=="VTubeStudioPublicAPI" and len(message["requestID"])<=64
    available=discover_ids({"defaultParameters":[{"name":"FaceAngleX"},{"name":"MouthOpen"}],"customParameters":[{"name":"NyraThinking"}]})
    mapping=resolve_mapping(available)
    assert mapping["head_x"]==["FaceAngleX"] and mapping["mouth_open"]==["MouthOpen"]
    assert "ParamAngleX" not in mapping["head_x"]
    values=parameter_values(AvatarState(head_x=.5,mouth_open=.4,neural_link="thinking"),mapping)
    assert {item["id"] for item in values}=={"FaceAngleX","MouthOpen","NyraThinking"}


def test_vts_discovers_standard_eye_and_head_cursor_parameters():
    mapping = resolve_mapping({"ParamEyeBallX", "ParamEyeBallY", "ParamAngleX", "ParamAngleY"})
    assert mapping["eye_x"] == ["ParamEyeBallX"]
    assert mapping["eye_y"] == ["ParamEyeBallY"]
    assert mapping["head_x"] == ["ParamAngleX"]
    assert mapping["head_y"] == ["ParamAngleY"]


@pytest.mark.asyncio
async def test_vts_cursor_keeps_eyes_stronger_than_head(monkeypatch):
    provider = VTubeStudioAvatarProvider(VTubeStudioConfig(enabled=True, renderer="LIVE2D"))
    provider.state = VTSConnectionState.MODEL_LOADED
    provider.mapping = {"eye_x": ["ParamEyeBallX"], "eye_y": ["ParamEyeBallY"], "head_x": ["ParamAngleX"], "head_y": ["ParamAngleY"]}
    captured = []

    async def capture(state):
        captured.append(state)
        provider.last_update += 1

    monkeypatch.setattr(provider, "apply", capture)
    assert await provider.apply_cursor(AvatarState(expression="focused"), 1, -.5)
    state = captured[0]
    assert state.expression == "focused"
    assert state.eye_x == pytest.approx(.88)
    assert state.eye_y == pytest.approx(-.36)
    assert state.head_x == pytest.approx(.16)
    assert state.head_y == pytest.approx(-.06)


def test_vts_token_is_local_and_round_trips(tmp_path: Path):
    auth=VTSAuth(object(),tmp_path/"secrets"/"token.json")
    assert auth.load_token() is None
    auth.save_token("private-token")
    assert auth.load_token()=="private-token"


@pytest.mark.asyncio
async def test_vts_connect_auth_parameters_and_model(monkeypatch):
    provider=VTubeStudioAvatarProvider(VTubeStudioConfig(enabled=True))
    class FakeAuth:
        def load_token(self): return "token"
        async def state(self): return {"active":True}
        async def authenticate(self,token): return True
    class FakeClient:
        socket=object()
        async def connect(self): pass
        async def close(self): self.socket=None
        async def call(self,kind,data=None):
            if kind=="InputParameterListRequest": return {"data":{"defaultParameters":[{"name":"MouthOpen"},{"name":"FaceAngleX"}]}}
            if kind=="CurrentModelRequest": return {"data":{"modelLoaded":True,"modelName":"Development Model"}}
            return {"data":{}}
    provider.client=FakeClient(); provider.auth=FakeAuth()
    status=await provider.connect()
    assert status["state"]==VTSConnectionState.READY and status["authenticated"] and status["parameter_count"]==2


@pytest.mark.asyncio
async def test_vts_auth_required_and_malformed_update_falls_back():
    provider=VTubeStudioAvatarProvider(VTubeStudioConfig(enabled=True))
    class NoToken:
        def load_token(self): return None
        async def state(self): return {"active":True}
    class BrokenClient:
        socket=object()
        async def connect(self): pass
        async def close(self): self.socket=None
        async def call(self,*args,**kwargs): raise ValueError("malformed")
    provider.client=BrokenClient(); provider.auth=NoToken()
    assert (await provider.connect())["state"]==VTSConnectionState.AUTH_REQUIRED
    provider.state=VTSConnectionState.READY; provider.authenticated=True; provider.mapping={"mouth_open":["MouthOpen"]}
    await provider.apply(AvatarState(mouth_open=.5))
    assert provider.state==VTSConnectionState.RECONNECTING


def test_brain_selection_requires_confirmation_and_preserves_fallback():
    brain=BrainManager("http://127.0.0.1:11434","qwen3:8b")
    with pytest.raises(PermissionError): brain.select_official("qwen3.5:9b",False)
    brain.use_temporarily("qwen3.5:9b")
    assert brain.active_model=="qwen3.5:9b" and brain.fallback_model=="qwen3:8b"
    brain.restore_official(); assert brain.active_model==brain.official_model
    with pytest.raises(ValueError): brain.use_temporarily("arbitrary:latest")


@pytest.mark.asyncio
async def test_brain_ready_tracks_active_model_residency(monkeypatch):
    brain = BrainManager("http://127.0.0.1:11434", "qwen3:8b")

    class FakeProvider:
        async def ready(self):
            return True

    monkeypatch.setattr(brain, "_provider", lambda model: FakeProvider())
    assert await brain.ready() is True


def test_ollama_residency_accepts_api_ps_name_or_model_fields():
    assert OllamaProvider._is_resident([{"name": "qwen3:8b"}], "qwen3:8b")
    assert OllamaProvider._is_resident([{"model": "qwen3:8b"}], "qwen3:8b")
    assert not OllamaProvider._is_resident([{"name": "qwen3.5:9b"}], "qwen3:8b")


@pytest.mark.asyncio
async def test_ollama_complete_retries_single_empty_message(monkeypatch):
    provider = OllamaProvider("http://127.0.0.1:11434", "qwen3:8b")
    calls = {"count": 0}
    mode = {"empty": False}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            calls["count"] += 1
            if mode["empty"] or calls["count"] == 1:
                return {"message": {"content": ""}}
            return {"message": {"content": "resposta real"}}

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs): return FakeResponse()

    monkeypatch.setattr("app.llm.ollama.httpx.AsyncClient", FakeClient)
    from app.llm.base import LLMMessage
    result = await provider.complete([LLMMessage(role="user", content="oi")])
    assert result.content == "resposta real"
    assert calls["count"] == 2

    calls["count"] = 0
    mode["empty"] = True
    with pytest.raises(RuntimeError, match="neither content nor tool calls"):
        await provider.complete([LLMMessage(role="user", content="oi")])
    assert calls["count"] == 2

