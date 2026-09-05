"""Local diagnostics and bounded controls for Emotional Presence Sync V1."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.emotional_presence.models import EmotionalPresenceSettingsUpdate
from app.persona_runtime.models import NyraEmotion


router = APIRouter(prefix="/emotional-presence", tags=["emotional-presence"])


class ControlledEmotionRequest(BaseModel):
    intensity: float = Field(default=.35, ge=0.0, le=.65)


def service(request: Request):
    value = getattr(request.app.state.services, "emotional_presence", None)
    if value is None:
        raise HTTPException(status_code=503, detail="Emotional Presence indisponível")
    return value


@router.get("/status")
async def status(request: Request):
    return await service(request).status()


@router.put("/settings")
async def settings(payload: EmotionalPresenceSettingsUpdate, request: Request):
    return await service(request).update_settings(payload)


@router.post("/test/{emotion}")
async def controlled_emotion(emotion: str, payload: ControlledEmotionRequest, request: Request):
    try:
        selected = NyraEmotion(emotion.casefold())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Emoção inválida") from exc
    await service(request).controlled_transition(selected, payload.intensity)
    return await service(request).status()
