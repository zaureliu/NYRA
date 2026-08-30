from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.intelligence.models import AutonomousTaskSpec, MemoryWrite


router = APIRouter(prefix="/intelligence", tags=["intelligence-v2"])


def platform(request: Request):
    value = getattr(request.app.state.services, "intelligence", None)
    if value is None:
        raise HTTPException(503, "Intelligence Platform unavailable")
    return value


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=50)
    project: str | None = Field(default=None, max_length=240)


class IngestRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    tree: bool = False
    max_files: int = Field(default=200, ge=1, le=1000)


class EvaluationRequest(BaseModel):
    scenarios: list[str] | None = Field(default=None, max_length=100)


class VisionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    prompt: str = Field(default="Descreva as evidências visuais relevantes.", min_length=1, max_length=2000)


@router.get("/status")
async def status(request: Request):
    return await platform(request).status()


@router.get("/capabilities")
async def capabilities(request: Request):
    return await platform(request).capabilities.snapshot()


@router.get("/capabilities/summary")
async def capability_summary(request: Request):
    return await platform(request).capabilities.natural_summary()


@router.get("/skills")
async def skills(request: Request):
    return await platform(request).skills.list()


@router.post("/memory")
async def memory_write(payload: MemoryWrite, request: Request):
    return await platform(request).memory.write(payload)


@router.post("/memory/search")
async def memory_search(payload: QueryRequest, request: Request):
    values = await platform(request).memory.retrieve(payload.query, project=payload.project, limit=payload.limit)
    return {"results": [item.model_dump(mode="json") for item in values]}


@router.delete("/memory/{memory_id}")
async def memory_delete(memory_id: str, request: Request):
    if not await platform(request).memory.delete(memory_id):
        raise HTTPException(404, "Memory not found")
    return {"deleted": True, "memory_id": memory_id}


@router.post("/rag/ingest")
async def rag_ingest(payload: IngestRequest, request: Request):
    engine = platform(request).knowledge
    try:
        if payload.tree:
            return await engine.ingest_tree(Path(payload.path), max_files=payload.max_files)
        return await engine.ingest(Path(payload.path))
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/rag/search")
async def rag_search(payload: QueryRequest, request: Request):
    values = await platform(request).knowledge.retrieve(payload.query, limit=payload.limit)
    return {"results": [item.model_dump(mode="json") for item in values]}


@router.post("/context/assemble")
async def context_assemble(payload: QueryRequest, request: Request):
    value = await platform(request).context.assemble(payload.query, project=payload.project)
    return value.model_dump(mode="json")


@router.post("/model/route")
async def model_route(payload: QueryRequest, request: Request):
    value = await platform(request).router.route(payload.query)
    return value.model_dump(mode="json")


@router.get("/tasks")
async def tasks(request: Request, include_terminal: bool = True):
    values = await platform(request).tasks.list(include_terminal=include_terminal)
    return {"tasks": [item.model_dump(mode="json") for item in values]}


@router.post("/tasks")
async def task_create(payload: AutonomousTaskSpec, request: Request):
    try:
        value = await platform(request).tasks.create(payload, approved=False)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return value.model_dump(mode="json")


@router.post("/tasks/{task_id}/run")
async def task_run(task_id: str, request: Request):
    try:
        value = await platform(request).tasks.run_now(task_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (PermissionError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error
    return value.model_dump(mode="json")


@router.post("/tasks/{task_id}/{action}")
async def task_control(task_id: str, action: str, request: Request):
    operations = {"cancel": platform(request).tasks.cancel, "pause": platform(request).tasks.pause,
                  "resume": platform(request).tasks.resume}
    operation = operations.get(action)
    if operation is None:
        raise HTTPException(400, "Unsupported task action")
    if not await operation(task_id):
        raise HTTPException(404, "Task not found or transition invalid")
    return {"success": True, "task_id": task_id, "action": action}


@router.get("/events")
async def events(request: Request, limit: int = 100):
    return {"events": await platform(request).events.recent(limit=min(500, max(1, limit)))}


@router.get("/incidents")
async def incidents(request: Request, limit: int = 30):
    return {"incidents": await platform(request).events.incidents(limit=min(100, max(1, limit)))}


@router.post("/diagnostics/{domain}")
async def diagnostics(domain: str, request: Request):
    try:
        value = await platform(request).diagnostics.run(domain)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    return value.model_dump(mode="json")


@router.get("/traces")
async def traces(request: Request, limit: int = 100):
    return {"traces": await platform(request).traces.recent(min(500, max(1, limit)))}


@router.post("/traces/{trace_id}/replay")
async def replay(trace_id: str, request: Request):
    return await platform(request).traces.replay(trace_id, dry_run=True)


@router.post("/evaluations/run")
async def evaluations(payload: EvaluationRequest, request: Request):
    return await platform(request).evaluations.run(payload.scenarios)


@router.post("/vision/analyze")
async def vision_analyze(payload: VisionRequest, request: Request):
    try:
        return await platform(request).vision.analyze_path(Path(payload.path), prompt=payload.prompt)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error
