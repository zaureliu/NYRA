"""Read-mostly local diagnostics for the persona runtime."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.persona_runtime.models import RelationshipEvidence


router = APIRouter(prefix="/persona-runtime", tags=["persona-runtime-v1"])


def service(request: Request):
    value = getattr(request.app.state.services, "persona_runtime", None)
    if value is None:
        raise HTTPException(503, "Persona runtime unavailable")
    return value


@router.get("/status")
async def status(request: Request):
    return await service(request).status()


@router.get("/context-preview")
async def context_preview(request: Request, q: str = ""):
    value = await service(request).build_context(q[:500])
    return {"context": value, "characters": len(value)}


@router.post("/relationship/evidence")
async def relationship_evidence(payload: RelationshipEvidence, request: Request):
    try:
        return await service(request).record_relationship_evidence(payload)
    except PermissionError as error:
        raise HTTPException(409, str(error)) from error
