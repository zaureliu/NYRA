"""Task Planner V2 + Workflow Memory tests (spec Partes G/J/W/Z)."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from app.tools.registry import ToolRegistry, create_tool_registry


def _registry_with_fs(tmp_path):
    """Real ToolRegistry seeded with filesystem tools bound to a temp dir."""
    from pathlib import Path

    registry = ToolRegistry()

    async def fs_write(path: str, content: str = "", **_):
        Path(path).write_text(content, encoding="utf-8")
        return {"success": True, "path": path, "bytes": len(content),
                "effect_verified": Path(path).is_file(), "verification_status": "VERIFIED"}

    async def fs_append(path: str, content: str = "", **_):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(content)
        return {"success": True, "effect_verified": True, "verification_status": "VERIFIED"}

    async def fs_read(path: str, **_):
        text = Path(path).read_text(encoding="utf-8")
        return {"success": True, "content": text[:2000], "exists": True,
                "effect_verified": True, "verification_status": "VERIFIED"}

    class _Model:
        pass

    from pydantic import BaseModel

    class WriteInput(BaseModel):
        path: str
        content: str = ""

    class ReadInput(BaseModel):
        path: str

    from app.tools.models import RiskLevel
    from app.tools.registry import ToolDefinition

    registry.register(ToolDefinition("fs_write_test", "escreve", RiskLevel.LOW_RISK,
                                     WriteInput,
                                     lambda path, content="", **_: fs_write(path, content)))
    registry.register(ToolDefinition("fs_read_test", "le", RiskLevel.READ_ONLY,
                                     ReadInput,
                                     lambda path, **_: fs_read(path)))
    registry.register(ToolDefinition("fs_append_test", "anexa", RiskLevel.LOW_RISK,
                                     WriteInput,
                                     lambda path, content="", **_: fs_append(path, content)))
    return registry


# ------------------------------------------------------------------ Parte G/W
@pytest.mark.asyncio
async def test_multi_step_task_real_execution_and_progress(tmp_path):
    registry = _registry_with_fs(tmp_path)
    from app.operator.tasks import OperatorTaskManager

    manager = OperatorTaskManager(registry, database_path=tmp_path / "tasks.db")
    await manager.initialize()
    target = tmp_path / "relatorio.txt"
    outcome = await manager.create_task(
        "Prepara relatório de teste",
        [
            {"step_id": "s1", "tool": "fs_write_test",
             "params": {"path": str(target), "content": "linha1\n"}},
            {"step_id": "s2", "tool": "fs_append_test",
             "params": {"path": str(target), "content": "linha2\n"},
             "depends_on": ["s1"]},
            {"step_id": "s3", "tool": "fs_read_test",
             "params": {"path": str(target)},
             "depends_on": ["s2"],
             "verification": {"required": True, "expect_contains": "linha2"}},
        ],
        verification_plan="arquivo contém duas linhas",
        deadline_seconds=120,
    )
    assert outcome["success"] is True, outcome
    task_id = outcome["task"]["task_id"]
    run = await manager.run_task(task_id)
    assert run["success"] is True

    deadline = time.time() + 20
    status = {}
    while time.time() < deadline:
        status = await manager.status(task_id)
        if status["task"]["state"] in {"SUCCEEDED", "FAILED"}:
            break
        await asyncio.sleep(0.4)
    final = status["task"]
    assert final["state"] == "SUCCEEDED", final
    assert final["progress"]["label"] == "3/3 steps"  # §154
    assert target.read_text(encoding="utf-8").splitlines() == ["linha1", "linha2"]


@pytest.mark.asyncio
async def test_task_transient_failure_retries_then_succeeds(tmp_path):
    """§293: falha transitória é retentada dentro do cap."""
    from pathlib import Path

    from pydantic import BaseModel

    from app.operator.tasks import OperatorTaskManager
    from app.tools.models import RiskLevel
    from app.tools.registry import ToolDefinition
    from app.tools.registry import ToolRegistry

    flaky = {"calls": 0}
    registry = ToolRegistry()

    class Empty(BaseModel):
        pass

    async def unstable(**_):
        flaky["calls"] += 1
        if flaky["calls"] < 3:
            return {"success": False, "error_code": "TRANSIENT", "message": "rede instável"}
        return {"success": True, "effect_verified": True}

    registry.register(ToolDefinition("unstable_probe", "instável", RiskLevel.LOW_RISK, Empty, unstable))
    manager = OperatorTaskManager(registry, database_path=tmp_path / "t.db")
    await manager.initialize()
    created = await manager.create_task("testa retry", [
        {"step_id": "only", "tool": "unstable_probe"},
    ])
    task_id = created["task"]["task_id"]
    await manager.run_task(task_id)
    deadline = time.time() + 15
    while time.time() < deadline:
        status = (await manager.status(task_id))["task"]
        if status["state"] in {"SUCCEEDED", "FAILED"}:
            break
        await asyncio.sleep(0.3)
    assert status["state"] == "SUCCEEDED"
    assert flaky["calls"] == 3  # 2 falhas + 1 sucesso (cap §152 respeitado)


@pytest.mark.asyncio
async def test_task_cancel_stops_work(tmp_path):
    registry = _registry_with_fs(tmp_path)
    from app.operator.tasks import OperatorTaskManager

    manager = OperatorTaskManager(registry, database_path=tmp_path / "t.db")
    await manager.initialize()
    created = await manager.create_task("tarefa longa", [
        {"step_id": "s1", "tool": "fs_write_test",
         "params": {"path": str(tmp_path / "a.txt"), "content": "x"}},
    ])
    task_id = created["task"]["task_id"]
    cancelled = await manager.cancel(task_id)
    assert cancelled["success"] is True
    assert cancelled["task"]["state"] == "CANCELLED"


# ------------------------------------------------------------------ Parte J/Z
@pytest.mark.asyncio
async def test_workflow_create_validate_version_and_run(tmp_path):
    registry = _registry_with_fs(tmp_path)
    from app.operator.workflows import WorkflowEngine, WorkflowDefinition, WorkflowStep

    engine = WorkflowEngine(registry, store_path=tmp_path / "workflows.json")
    target = tmp_path / "ambiente-teste.txt"

    steps = [
        WorkflowStep(step_id="criar", tool="fs_write_test",
                     params={"path": "{alvo}", "content": "ambiente teste ok"}),
        WorkflowStep(step_id="checar", tool="fs_read_test", params={"path": "{alvo}"},
                     depends_on=["criar"]),
    ]
    definition = WorkflowDefinition(
        workflow_id="wf_ambiente_teste",
        name="Abrir ambiente teste",
        description="Cria e verifica arquivo do ambiente de teste",
        trigger_phrases=["abrir ambiente teste"],
        steps=steps,
        parameters={"alvo": str(target)},
        risk="LOW_RISK",
    )
    created = await engine.create(definition)
    assert created["success"] is True, created

    # §199: tool desconhecida é recusada na validação.
    bad = definition.model_copy(deep=True)
    bad.steps[0].tool = "tool_que_nao_existe"
    invalid = await engine.create(bad)
    assert invalid["success"] is False and invalid["error_code"] == "VALIDATION_FAILED"

    # §198: update incrementa versão.
    updated = await engine.update("wf_ambiente_teste", {"description": "v2"})
    assert updated["success"] is True and updated["workflow"]["version"] == 2

    # §200: dry run não executa nada.
    dry = engine.dry_run("wf_ambiente_teste")
    assert dry["success"] is True and not target.exists()
    assert dry["plan"][0]["tool"] == "fs_write_test"

    # §191/§202: executa com grounding normal.
    run = await engine.run("wf_ambiente_teste")
    assert run["success"] is True, run
    assert run["executed_steps"][0]["ok"] is True
    assert target.read_text(encoding="utf-8") == "ambiente teste ok"

    # Persistência sobrevive a nova instância (§148 análogo).
    reloaded = WorkflowEngine(registry, store_path=tmp_path / "workflows.json")
    assert reloaded.get("wf_ambiente_teste").version == 2

    # Trigger phrase match (§195).
    found = reloaded.find_by_trigger("quero abrir ambiente teste agora")
    assert found is not None and found.workflow_id == "wf_ambiente_teste"


@pytest.mark.asyncio
async def test_workflow_missing_parameter_fails_closed(tmp_path):
    registry = _registry_with_fs(tmp_path)
    from app.operator.workflows import WorkflowEngine, WorkflowDefinition, WorkflowStep

    engine = WorkflowEngine(registry, store_path=tmp_path / "wf.json")
    definition = WorkflowDefinition(
        workflow_id="wf_param_obrigatorio",
        name="Precisa parâmetro",
        steps=[WorkflowStep(step_id="s1", tool="fs_write_test",
                            params={"path": "{destino}", "content": "x"})],
    )
    await engine.create(definition)
    run = await engine.run("wf_param_obrigatorio")  # sem 'destino'
    assert run["success"] is False
    assert run["error_code"] == "MISSING_PARAMETERS"
