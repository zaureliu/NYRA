from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from app.events import EventBus, EventType
from app.intelligence.capabilities import CapabilityRegistryV2
from app.intelligence.context import ContextEngine
from app.intelligence.api import router as intelligence_router
from app.intelligence.knowledge import KnowledgeEngine
from app.intelligence.memory import MemoryV2Service
from app.intelligence.models import AutonomousTaskSpec
from app.intelligence.storage import IntelligenceStore
from app.intelligence.tasks import AutonomousTaskEngine
from app.intelligence.tracing import TraceService
from app.open_loops import (
    ArtifactReference,
    OpenLoopCreate,
    OpenLoopEngine,
    OpenLoopState,
    OpenLoopType,
    ResolutionEvidence,
)
from app.open_loops.models import utc_now
from app.world_state import WorldStateEngine


async def make_engine(tmp_path, *, bus: EventBus | None = None):
    store = IntelligenceStore(tmp_path / "nyra.db")
    await store.initialize()
    memory = MemoryV2Service(store)
    events = bus or EventBus()
    engine = OpenLoopEngine(store, memory, events)
    await engine.initialize()
    return store, memory, events, engine


async def wait_for_loop(engine: OpenLoopEngine, relation: str, *, waiting: bool = False):
    for _ in range(40):
        values = await (engine.get_waiting_loops() if waiting else engine.list())
        match = next((item for item in values if relation in item.related_monitor), None)
        if match is not None:
            return match
        await asyncio.sleep(.05)
    raise AssertionError(f"open loop relation not observed: {relation}")


async def wait_for_task_loop(engine: OpenLoopEngine, relation: str):
    for _ in range(80):
        match = next((item for item in await engine.list() if relation in item.related_task), None)
        if match is not None:
            return match
        await asyncio.sleep(.05)
    raise AssertionError(f"task open loop relation not observed: {relation}")


async def wait_for_state(engine: OpenLoopEngine, loop_id: str, state: OpenLoopState):
    for _ in range(80):
        value = await engine.get(loop_id)
        if value is not None and value.state == state:
            return value
        await asyncio.sleep(.05)
    raise AssertionError(f"open loop did not reach {state}: {loop_id}")


@pytest.mark.asyncio
async def test_create_goal_dedup_and_false_positive_policy(tmp_path):
    _store, _memory, _bus, engine = await make_engine(tmp_path)
    first, first_dedup = await engine.create(OpenLoopCreate(
        title="Corrigir voz da NYRA", goal="Melhorar voz", related_project="nyra",
        type=OpenLoopType.GOAL,
    ))
    second, second_dedup = await engine.create(OpenLoopCreate(
        title="Testar voz", goal="Melhorar voz", related_project="nyra",
        type=OpenLoopType.PENDING_INTENTION,
    ))
    third, third_dedup = await engine.create(OpenLoopCreate(
        title="A voz ainda está ruim", goal="Melhorar voz", related_project="nyra",
        type=OpenLoopType.BLOCKED_WORK,
    ))
    assert first_dedup is False
    assert second_dedup is True and second.id == first.id
    assert third_dedup is True and third.id == first.id
    assert len(await engine.list()) == 1
    assert len(await engine.list_goals()) == 1
    distinct, distinct_dedup = await engine.create(OpenLoopCreate(
        title="Validar condição da voz NYRA", goal="Validar monitor de voz",
        related_project="nyra", type=OpenLoopType.WAITING_CONDITION,
        state=OpenLoopState.WAITING,
    ))
    assert distinct_dedup is False and distinct.id != first.id
    assert len(await engine.list()) == 2
    assert await engine.observe_user_intention("oi", source_turn="turn_hello") is None
    assert await engine.observe_user_intention("abre Discord", source_turn="turn_open") is None
    assert await engine.observe_user_intention("valeu", source_turn="turn_thanks") is None
    with pytest.raises(PermissionError, match="SECRET_REJECTED"):
        await engine.create(OpenLoopCreate(
            title="Depois testo token=abcdefghijklmnop", goal="Não persistir segredo",
        ))
    await engine.stop()


@pytest.mark.asyncio
async def test_waiting_blocked_resume_cancel_stale_and_restart(tmp_path):
    store, memory, bus, engine = await make_engine(tmp_path)
    waiting, _ = await engine.create(OpenLoopCreate(
        title="VM 120 precisa voltar online", type=OpenLoopType.WAITING_CONDITION,
        state=OpenLoopState.WAITING, goal="Recuperar VM 120", priority=90,
        waiting_for={"kind": "external_condition", "description": "VM 120 online"},
        context={"last_confirmed_state": "VM 120 offline", "last_action": "probe read-only"},
        next_possible_action="Conferir a leitura do monitor.",
    ))
    blocked = await engine.transition(
        waiting.id, OpenLoopState.BLOCKED, reason="monitor indisponível", actor="monitor_job",
    )
    assert blocked.state == OpenLoopState.BLOCKED
    resumed = await engine.resume_context("retoma aquilo", activate=True)
    assert resumed is not None
    assert resumed.objective == waiting.title
    assert resumed.last_action == "probe read-only"
    assert resumed.blocker == "monitor indisponível"
    assert resumed.next_possible_action == "Conferir a leitura do monitor."

    terminal, _ = await engine.create(OpenLoopCreate(
        title="Loop encerrado mais recente", goal="Objetivo já encerrado", priority=100,
    ))
    await engine.resolve(terminal.id, ResolutionEvidence(
        kind="operator_confirmation", source="operator_test", verified=True,
    ), actor="operator")
    generic = await engine.resume_context("retoma aquilo", activate=False)
    assert generic is not None and generic.objective == waiting.title

    await engine.stop()
    restarted = OpenLoopEngine(store, memory, bus)
    await restarted.initialize()
    restored = await restarted.get(waiting.id)
    assert restored is not None and restored.state == OpenLoopState.ACTIVE

    await restarted.cancel(waiting.id)
    cancelled = await restarted.get(waiting.id)
    assert cancelled is not None and cancelled.state == OpenLoopState.CANCELLED

    old, _ = await restarted.create(OpenLoopCreate(
        title="Documentação ainda incompleta", goal="Completar documentação",
    ))
    count = await restarted.apply_stale_policy(now=utc_now() + timedelta(days=100))
    assert count == 1
    assert (await restarted.get(old.id)).state == OpenLoopState.STALE
    await restarted.stop()


@pytest.mark.asyncio
async def test_resolution_requires_grounded_evidence_and_writes_memory_v2(tmp_path):
    _store, memory, _bus, engine = await make_engine(tmp_path)
    loop, _ = await engine.create(OpenLoopCreate(
        title="Corrigir o bug de áudio", goal="Áudio estável",
        related_task=["task_audio"], related_project="nyra",
    ))
    with pytest.raises(ValueError, match="EVIDENCE_REQUIRED"):
        await engine.transition(loop.id, OpenLoopState.RESOLVED, reason="LLM disse resolvido")
    with pytest.raises(ValueError, match="LLM_CANNOT_RESOLVE"):
        await engine.resolve(loop.id, ResolutionEvidence(
            kind="task_effect_verified", source="llm", verified=True,
            reference_id="task_audio",
        ))
    with pytest.raises(ValueError, match="REFERENCE_MISMATCH"):
        await engine.resolve(loop.id, ResolutionEvidence(
            kind="task_effect_verified", source="task_engine", verified=True,
            reference_id="task_other",
        ))
    with pytest.raises(ValueError, match="SERVER_OWNED"):
        await engine.transition(
            loop.id, OpenLoopState.RESOLVED, reason="forged api evidence",
            evidence=ResolutionEvidence(
                kind="task_effect_verified", source="task_engine", verified=True,
                reference_id="task_audio",
            ), actor="operator",
        )
    resolved = await engine.resolve(loop.id, ResolutionEvidence(
        kind="task_effect_verified", source="task_engine", verified=True,
        reference_id="task_audio", detail={"effect_verified": True},
    ))
    assert resolved.state == OpenLoopState.RESOLVED
    memories = await memory.retrieve("Open loop resolvido bug áudio", project="nyra")
    assert memories and memories[0].category == "open_loop_resolution"
    assert memories[0].provenance["loop_id"] == loop.id
    await engine.stop()


@pytest.mark.asyncio
async def test_task_engine_link_and_effect_verification(tmp_path):
    store, _memory, bus, loops = await make_engine(tmp_path)
    tasks = AutonomousTaskEngine(store, CapabilityRegistryV2(), TraceService(store), event_bus=bus)

    async def verified(_):
        return {"success": True, "effect_verified": True}

    tasks.register("verified_open_loop", verified)
    task = await tasks.create(AutonomousTaskSpec(
        title="Gerar relatório", objective="Gerar relatório verificado",
        action="verified_open_loop",
    ))
    linked = await wait_for_task_loop(loops, task.task_id)
    assert linked.state == OpenLoopState.OPEN
    completed = await tasks.run_now(task.task_id)
    assert completed.result["effect_verified"] is True
    linked = await wait_for_state(loops, linked.id, OpenLoopState.RESOLVED)
    assert linked is not None and linked.state == OpenLoopState.RESOLVED
    assert linked.resolution_evidence[-1].reference_id == task.task_id
    await loops.stop()


@pytest.mark.asyncio
async def test_monitor_waiting_condition_and_timeout_grounding(tmp_path):
    _store, _memory, bus, engine = await make_engine(tmp_path)
    await bus.publish(
        EventType.MONITOR_JOB_CREATED, monitor_id="mon_vm", objective="VM 120 online",
        status="ACTIVE", source_turn_id="turn_vm",
        condition={"path": "online", "operator": "TRUTHY"},
    )
    loop = await wait_for_loop(engine, "mon_vm", waiting=True)
    await bus.publish(
        EventType.MONITOR_JOB_COMPLETED, monitor_id="mon_vm", objective="VM 120 online",
        status="COMPLETED", completion_reason="CONDITION_MET",
        last_reading={"ok": True, "value": True, "observed_at": 123.0},
    )
    assert (await wait_for_state(engine, loop.id, OpenLoopState.RESOLVED)).state == OpenLoopState.RESOLVED

    await bus.publish(
        EventType.MONITOR_JOB_CREATED, monitor_id="mon_download", objective="Download terminar",
        status="ACTIVE", condition={"path": "done", "operator": "TRUTHY"},
    )
    timed = await wait_for_loop(engine, "mon_download", waiting=True)
    await bus.publish(
        EventType.MONITOR_JOB_COMPLETED, monitor_id="mon_download", objective="Download terminar",
        status="COMPLETED", completion_reason="TIMEOUT",
        last_reading={"ok": True, "value": False, "observed_at": 124.0},
    )
    assert (await wait_for_state(engine, timed.id, OpenLoopState.BLOCKED)).state == OpenLoopState.BLOCKED

    await bus.publish(
        EventType.MONITOR_JOB_CREATED, monitor_id="mon_resume", objective="VM voltar",
        status="ACTIVE", condition={"path": "online", "operator": "TRUTHY"},
    )
    resumable = await wait_for_loop(engine, "mon_resume", waiting=True)
    resumable.context["resolve_on_condition"] = False
    resumable.next_possible_action = "Retomar a configuração da VM."
    await engine._save_loop(resumable)
    await bus.publish(
        EventType.MONITOR_JOB_COMPLETED, monitor_id="mon_resume", objective="VM voltar",
        status="COMPLETED", completion_reason="CONDITION_MET",
        last_reading={"ok": True, "value": True, "observed_at": 125.0},
    )
    active = await wait_for_state(engine, resumable.id, OpenLoopState.ACTIVE)
    assert active is not None and active.state == OpenLoopState.ACTIVE
    assert active.resolution_evidence[-1].kind == "monitor_condition_reached"
    await engine.stop()


@pytest.mark.asyncio
async def test_artifact_link_and_artifact_existence_resolution(tmp_path):
    _store, _memory, bus, engine = await make_engine(tmp_path)
    path = str(tmp_path / "report.pdf")
    loop, _ = await engine.create(OpenLoopCreate(
        title="Aguardar relatório PDF", state=OpenLoopState.WAITING,
        type=OpenLoopType.WAITING_CONDITION,
        waiting_for={"kind": "artifact_exists", "path": path,
                     "description": "relatório existir"},
        related_artifact=[ArtifactReference(path=path, exists_state="planned")],
    ))
    await bus.publish(
        EventType.ARTIFACT_CONTEXT_UPDATED, verified=True,
        artifact={"artifact_id": "artifact_report", "path": path,
                  "kind": "pdf", "exists_state": "verified"},
    )
    resolved = await wait_for_state(engine, loop.id, OpenLoopState.RESOLVED)
    assert resolved is not None and resolved.state == OpenLoopState.RESOLVED
    assert resolved.related_artifact[0].artifact_id == "artifact_report"
    assert resolved.resolution_evidence[-1].kind == "artifact_verified"
    await engine.stop()


@pytest.mark.asyncio
async def test_context_chat_world_state_and_no_execution_authority(tmp_path):
    bus = EventBus()
    world = WorldStateEngine(bus, persistence_path=tmp_path / "world-state.json")
    await world.start()
    store, memory, _bus, loops = await make_engine(tmp_path, bus=bus)
    created = await loops.observe_user_intention(
        "Depois eu testo a integração Discord", source_turn="turn_discord", project="nyra",
    )
    assert created is not None
    assert await loops.chat_response("o que ficou pendente?") == (
        "Temos uma coisa em aberto: Depois eu testo a integração Discord."
    )
    resume = await loops.chat_response("retoma aquilo")
    assert resume and "não autoriza" not in resume.casefold()
    assert "políticas normais" in resume
    snapshot = world.get_snapshot()
    assert snapshot["active_goal"]["value"]
    assert snapshot["open_loop_count"]["value"] == 1
    assert snapshot["waiting_loop_count"]["value"] == 0
    assert snapshot["most_relevant_open_loop"]["value"]["title"] == created.title

    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    context = ContextEngine(
        memory, KnowledgeEngine(store, (knowledge_root,)), budget_characters=3000,
        world_state_provider=world.context_summary,
        open_loop_provider=loops.context_summary,
    )
    assembly = await context.assemble("continua de onde parou", include_runtime=False)
    block = next(item for item in assembly.blocks if item.source == "open_loops")
    assert "objective:" in block.content
    assert "authorization" in block.content
    assert block.provenance["authorization"] is False
    assert not hasattr(loops, "execute")
    await loops.stop()
    await world.stop()


@pytest.mark.asyncio
async def test_structured_blocked_and_selfdev_resolution(tmp_path):
    _store, _memory, bus, engine = await make_engine(tmp_path)
    await bus.publish(
        EventType.SELFDEV_ISSUE_DETECTED, issue_id="SELFDEV-TEST",
        title="Corrigir regressão de voz",
    )
    loop = await wait_for_task_loop(engine, "selfdev:SELFDEV-TEST")
    await bus.publish(EventType.SELFDEV_VALIDATION_FAIL, issue_id="SELFDEV-TEST")
    assert (await wait_for_state(engine, loop.id, OpenLoopState.BLOCKED)).state == OpenLoopState.BLOCKED
    await bus.publish(EventType.SELFDEV_PLAN_CREATED, issue_id="SELFDEV-TEST")
    assert (await wait_for_state(engine, loop.id, OpenLoopState.ACTIVE)).state == OpenLoopState.ACTIVE
    await bus.publish(EventType.SELFDEV_POST_VALIDATION_PASS, issue_id="SELFDEV-TEST")
    assert (await wait_for_state(engine, loop.id, OpenLoopState.RESOLVED)).state == OpenLoopState.RESOLVED
    await engine.stop()


@pytest.mark.asyncio
async def test_local_api_exposes_loops_and_keeps_actor_server_owned(tmp_path):
    _store, _memory, _bus, engine = await make_engine(tmp_path)
    app = FastAPI()
    app.state.services = SimpleNamespace(intelligence=SimpleNamespace(open_loops=engine))
    app.include_router(intelligence_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        created = await client.post("/intelligence/open-loops", json={
            "title": "Validar API de retomada", "goal": "Validar API",
            "related_task": ["task_api"],
        })
        assert created.status_code == 200
        loop_id = created.json()["open_loop"]["id"]
        other = await client.post("/intelligence/open-loops", json={
            "title": "Validar outra retomada parecida", "goal": "Outro objetivo",
            "priority": 100,
        })
        assert other.status_code == 200
        priority = await client.get("/intelligence/open-loops/priority")
        assert priority.status_code == 200
        assert loop_id in {item["id"] for item in priority.json()["open_loops"]}

        forged = await client.post(f"/intelligence/open-loops/{loop_id}/transition", json={
            "state": "RESOLVED", "reason": "forged",
            "evidence": {
                "kind": "task_effect_verified", "source": "task_engine",
                "verified": True, "reference_id": "task_api",
            },
        })
        assert forged.status_code == 409
        confirmed = await client.post(f"/intelligence/open-loops/{loop_id}/transition", json={
            "state": "RESOLVED", "reason": "operador confirmou",
            "evidence": {
                "kind": "operator_confirmation", "source": "operator_api",
                "verified": True, "reference_id": loop_id,
            },
        })
        assert confirmed.status_code == 200
        resumed = await client.post(f"/intelligence/open-loops/{loop_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["execution_authorized"] is False
        assert resumed.json()["resume_context"]["objective"] == "Validar API de retomada"
    await engine.stop()
