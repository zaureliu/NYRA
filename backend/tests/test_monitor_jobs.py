from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.events import EventBus, EventType
from app.operator.monitoring import (
    ConditionOperator,
    MonitorCondition,
    MonitorCreateRequest,
    MonitorJobError,
    MonitorJobManager,
    enforce_monitor_promise,
    is_monitor_cancel_request,
)
from app.tools.models import EmptyInput, RiskLevel
from app.tools.registry import ToolDefinition, ToolRegistry
from app.operator.tools_reg import _register_monitor_tools


def _request(*, objective: str = "latência abaixo de 200 ms", target=200,
             operator: ConditionOperator = ConditionOperator.LT,
             interval: float = 0.05, duration: float = 2.0,
             significant_change: float | None = 25.0) -> MonitorCreateRequest:
    # Production/API validation starts at one second. The engine accepts a
    # sub-second cadence here only to keep this focused lifecycle test short.
    return MonitorCreateRequest.model_construct(
        objective=objective,
        probe_tool="test_real_reading",
        probe_params={},
        condition=MonitorCondition(path="value", operator=operator, target=target),
        interval_seconds=interval,
        duration_seconds=duration,
        significant_change=significant_change,
        significant_change_percent=10.0,
        notification_cooldown_seconds=0.0,
        voice=False,
    )


def _registry(state_path) -> ToolRegistry:
    registry = ToolRegistry()

    async def read_state(**_):
        try:
            document = json.loads(await asyncio.to_thread(state_path.read_text, "utf-8"))
        except OSError as exc:
            return {
                "success": False,
                "error_code": "TEST_SOURCE_UNAVAILABLE",
                "message": type(exc).__name__,
            }
        return {"success": True, "value": document["value"]}

    registry.register(ToolDefinition(
        "test_real_reading", "Reads a real test state file.",
        RiskLevel.READ_ONLY, EmptyInput, read_state,
    ))
    return registry


async def _wait_status(manager: MonitorJobManager, monitor_id: str, expected: str,
                       timeout: float = 2.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current = await manager.status(monitor_id)
        if current["monitor"]["status"] == expected:
            return current["monitor"]
        await asyncio.sleep(0.02)
    raise AssertionError(f"MonitorJob {monitor_id} did not reach {expected}")


@pytest.mark.asyncio
async def test_monitor_runs_without_new_user_message_and_notifies_condition(tmp_path):
    state_path = tmp_path / "reading.json"
    state_path.write_text('{"value": 350}', encoding="utf-8")
    bus = EventBus(history_size=200)
    manager = MonitorJobManager(
        _registry(state_path), bus, database_path=tmp_path / "nyra.db",
    )
    await manager.initialize()
    manager.start()
    try:
        created = await manager.create(_request(), source_turn_id="turn_monitor_test")
        monitor_id = created["monitor"]["monitor_id"]
        assert created["monitor"]["status"] == "ACTIVE"
        assert created["monitor"]["last_reading"]["value"] == 350
        assert manager.has_job_for_turn("turn_monitor_test")

        # External state changes; no chat/user call advances the MonitorJob.
        state_path.write_text('{"value": 150}', encoding="utf-8")
        finished = await _wait_status(manager, monitor_id, "COMPLETED")
        assert finished["completion_reason"] == "CONDITION_MET"
        assert finished["last_reading"]["value"] == 150
        assert "150" in finished["final_summary"]
        deadline = asyncio.get_running_loop().time() + 1
        while EventType.MONITOR_JOB_COMPLETED not in [event.type for event in bus.history()]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        event_types = [event.type for event in bus.history()]
        assert EventType.MONITOR_JOB_COMPLETED in event_types
        notifications = [
            event for event in bus.history()
            if event.type == EventType.MONITOR_NOTIFICATION
        ]
        assert notifications[-1].payload["kind"] == "CONDITION_MET"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_active_monitor_is_recovered_after_manager_restart(tmp_path):
    state_path = tmp_path / "reading.json"
    state_path.write_text('{"value": 10}', encoding="utf-8")
    database = tmp_path / "nyra.db"
    registry = _registry(state_path)

    first = MonitorJobManager(registry, EventBus(), database_path=database)
    await asyncio.wait_for(first.initialize(), timeout=2)
    first.start()
    created = await asyncio.wait_for(first.create(_request(
        objective="valor chegar a 99", target=99,
        operator=ConditionOperator.GTE,
    )), timeout=2)
    monitor_id = created["monitor"]["monitor_id"]
    await asyncio.wait_for(first.shutdown(), timeout=2)  # ACTIVE is deliberately preserved on shutdown.

    resumed_bus = EventBus(history_size=200)
    resumed = MonitorJobManager(registry, resumed_bus, database_path=database)
    recovery = await asyncio.wait_for(resumed.initialize(), timeout=2)
    assert recovery["recovered"] == 1
    assert (await resumed.status(monitor_id))["monitor"]["status"] == "ACTIVE"
    resumed.start()
    try:
        state_path.write_text('{"value": 100}', encoding="utf-8")
        finished = await _wait_status(resumed, monitor_id, "COMPLETED")
        assert finished["sample_count"] >= 2
        assert finished["completion_reason"] == "CONDITION_MET"
    finally:
        await asyncio.wait_for(resumed.shutdown(), timeout=2)


@pytest.mark.asyncio
async def test_monitor_deduplicates_small_changes_reports_error_and_can_cancel_naturally(tmp_path):
    state_path = tmp_path / "reading.json"
    state_path.write_text('{"value": 10}', encoding="utf-8")
    bus = EventBus(history_size=300)
    manager = MonitorJobManager(
        _registry(state_path), bus, database_path=tmp_path / "nyra.db",
    )
    await manager.initialize()
    manager.start()
    try:
        created = await manager.create(_request(
            objective="temperatura do teste", target=999,
            operator=ConditionOperator.GT, significant_change=5,
        ))
        monitor_id = created["monitor"]["monitor_id"]
        state_path.write_text('{"value": 12}', encoding="utf-8")
        await asyncio.sleep(0.12)
        assert not any(
            event.type == EventType.MONITOR_JOB_CHANGED for event in bus.history()
        )
        state_path.write_text('{"value": 20}', encoding="utf-8")
        deadline = asyncio.get_running_loop().time() + 1
        while not any(event.type == EventType.MONITOR_JOB_CHANGED for event in bus.history()):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)

        state_path.unlink()
        deadline = asyncio.get_running_loop().time() + 1
        while not any(
            event.type == EventType.MONITOR_NOTIFICATION
            and event.payload.get("kind") == "ERROR"
            for event in bus.history()
        ):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)

        assert is_monitor_cancel_request("para de monitorar isso")
        cancelled = await manager.cancel_from_text("para de monitorar isso")
        assert cancelled["success"] is True
        assert cancelled["monitor"]["monitor_id"] == monitor_id
        assert cancelled["monitor"]["status"] == "CANCELLED"
        assert cancelled["monitor"]["final_summary"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_monitor_rejects_non_read_only_probe_and_promises_fail_closed(tmp_path):
    registry = ToolRegistry()

    async def mutate(**_):
        return {"success": True, "value": 1}

    registry.register(ToolDefinition(
        "unsafe_probe", "Not a valid monitor probe.",
        RiskLevel.LOW_RISK, EmptyInput, mutate,
    ))
    manager = MonitorJobManager(registry, EventBus(), database_path=tmp_path / "nyra.db")
    await manager.initialize()
    unsafe = _request().model_copy(update={"probe_tool": "unsafe_probe"})
    with pytest.raises(MonitorJobError) as failure:
        await manager.create(unsafe)
    assert failure.value.code == "MONITOR_PROBE_NOT_READ_ONLY"

    promise = "Vou monitorar a latência e aviso quando baixar."
    blocked = enforce_monitor_promise(promise, job_created=False)
    assert "não há monitoramento ativo" in blocked.casefold()
    assert enforce_monitor_promise(promise, job_created=True) == promise


@pytest.mark.asyncio
async def test_flat_llm_tool_schema_creates_the_structured_monitor(tmp_path):
    state_path = tmp_path / "reading.json"
    state_path.write_text('{"value": 42}', encoding="utf-8")
    registry = _registry(state_path)
    manager = MonitorJobManager(registry, EventBus(), database_path=tmp_path / "nyra.db")
    await manager.initialize()
    _register_monitor_tools(registry, SimpleNamespace(monitor_jobs=manager))

    schema = next(
        item["function"]["parameters"] for item in registry.llm_tools()
        if item["function"]["name"] == "monitor_create"
    )
    assert "condition_path" in schema["properties"]
    assert "condition" not in schema["properties"]
    result = await registry.execute("monitor_create", {
        "objective": "valor mudar",
        "probe_tool": "test_real_reading",
        "probe_params": {},
        "condition_path": "value",
        "condition_operator": "GTE",
        "target_value": 100,
        "interval_seconds": 1,
        "duration_seconds": 20,
        "notification_cooldown_seconds": 0,
        "voice": False,
    }, exposure="llm")
    assert result.ok is True
    assert result.data["effect_verified"] is True
    assert result.data["monitor"]["condition"] == {
        "path": "value", "operator": "GTE", "target": 100,
    }

    # qwen-style compact calls are normalized, but still require enough
    # information to identify a real probe and a structured condition.
    legacy = await registry.execute("monitor_create", {
        "probe_tool": "network_watch",
        "condition": "timestamp_changed",
        "target": "NetworkWatch",
        "interval": 2,
        "duration": 20,
    }, exposure="llm")
    # The alias resolves, then fails because this isolated registry deliberately
    # has no production get_network_status tool; it must not silently invent data.
    assert legacy.ok is False
    assert "READ_ONLY" in str(legacy.data.get("message") or "")
