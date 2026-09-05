from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.web_research.models import ResearchRequest


router = APIRouter(prefix='/hardware', tags=['hardware-engineering'])


def service(request):
    engine = getattr(request.app.state.services, 'hardware_engine', None)
    if engine is None:
        raise HTTPException(503, 'Hardware engine unavailable')
    return engine


class GoalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=3, max_length=1000)


class ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    full: bool


class ReferenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    board_id: str = Field(min_length=2, max_length=100)


@router.post('/reference-project')
async def reference_project(body: ReferenceRequest, request: Request):
    meta = await service(request).project_workflow.reference(body.board_id)
    return {'project': meta, 'origin': 'REFERENCE', 'physical_present': False}


@router.get('/reference-profile')
async def reference_profile(request: Request):
    response, profile = await service(request).project_workflow.reference_info('perfil de referência do projeto atual')
    return {'response': response, 'profile': profile.model_dump()}


@router.get('/status')
async def status(request: Request):
    return service(request).status()


@router.post('/discover')
async def discover(request: Request):
    return await service(request).discovery.refresh()


@router.post('/goals')
async def goal(body: GoalRequest, request: Request):
    return {'response': await service(request).handle(body.text)}


@router.put('/settings')
async def configure(body: ConfigureRequest, request: Request):
    # Existing loopback/bridge authentication middleware protects this API.
    # No LLM tool exposes configuration or grants FULL.
    return service(request).configure(body.full)


@router.post('/research')
async def research(body: ResearchRequest, request: Request):
    return await service(request).research.research(body)
