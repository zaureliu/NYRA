"""Local API for status, settings, audit and persistent notifications."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.proactive_presence.models import ProactiveSettingsUpdate


router = APIRouter(prefix="/proactive-presence", tags=["proactive-presence-v1"])


def service(request: Request):
    value = getattr(request.app.state.services, "proactive_presence", None)
    if value is None:
        raise HTTPException(503, "Proactive Presence unavailable")
    return value


@router.get("/status")
async def status(request: Request):
    return await service(request).status()


@router.get("/settings")
async def settings(request: Request):
    return service(request).settings.model_dump(mode="json")


@router.put("/settings")
async def update_settings(payload: ProactiveSettingsUpdate, request: Request):
    value = await service(request).update(payload)
    return value.model_dump(mode="json")


@router.get("/notifications")
async def notifications(request: Request, include_read: bool = True, limit: int = 100):
    values = await service(request).store.notifications(
        include_read=include_read, limit=max(1, min(limit, 500)),
    )
    return {"notifications": [item.model_dump(mode="json") for item in values]}


@router.post("/notifications/{notification_id}/read")
async def notification_read(notification_id: str, request: Request):
    if not await service(request).store.mark_read(notification_id):
        raise HTTPException(404, "Notification not found")
    return {"read": True, "notification_id": notification_id}


@router.get("/decisions")
async def decisions(request: Request, limit: int = 100):
    values = await service(request).store.recent_decisions(limit=max(1, min(limit, 500)))
    return {"decisions": [item.model_dump(mode="json") for item in values]}
