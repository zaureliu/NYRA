"""Production hardening suite (prompt10).

Cobertura: workflow engine v2 (§45-§58), health matrix/report (§11-§15, §218),
error envelope (§18), daily check fixtures (§240-§247), benchmark lab
(MODEL_NOT_INSTALLED §69, scoring determinístico §90-§97, profiles NOT
INSTALLED §99-§100), templates seed idempotente (§59-§62).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.operator.workflows import (
    RUN_STATE_FAILED,
    RUN_STATE_SUCCEEDED,
    RUN_STATE_WAITING_USER,
    STEP_ROLLED_BACK,
    STEP_VERIFIED,
    STEP_WAITING_USER,
    RetryPolicy,
    RollbackSpec,
    VerificationProbe,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStep,
    find_cycle,
    substitute_outputs,
    topological_order,
)


# --------------------------------------------------------------------- helpers


class FakeResult:
    def __init__(self, ok=True, data=None, risk="LOW_RISK"):
        self.ok = ok
        self.data = data if data is not None else {"success": ok}
        self.risk = risk


class FakeRegistry:
    """Tool registry de teste com latência/falhas controladas por script."""

    def __init__(self):
        self.scripts: dict[str, list] = {}
        self.calls: list[tuple[str, dict]] = []
        self.approvals: dict[str, str] = {}

    def set(self, tool: str, outcomes: list):
        self.scripts[tool] = list(outcomes)

    async def execute(self, name: str, payload: dict, *, exposure: str = "internal"):
        assert exposure in {"internal", "llm", "api"}
        self.calls.append((name, dict(payload)))
        queue = self.scripts.get(name)
        if not queue:
            return FakeResult(True, {"success": True})
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def descriptions(self):
        return [
            {"name": "probe_a", "risk": "READ_ONLY"},
            {"name": "mutator_b", "risk": "ELEVATED"},
            {"name": "rollback_c", "risk": "LOW_RISK"},
        ]

    def approval_granted(self, approval_id: str) -> bool:
        return self.approvals.get(approval_id) == "GRANTED"


def make_engine(tmp_path, registry=None) -> WorkflowEngine:
    store_module = __import__("app.operator.workflows", fromlist=["WorkflowRunStore"])
    engine = WorkflowEngine(
        registry,
        store_path=tmp_path / "workflows.json",
        history_store=store_module.WorkflowRunStore(tmp_path / "kazumi.db"),
    )
    return engine


def definition(steps, parameters=None, workflow_id="wf_teste", enabled=True) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        name="Teste",
        steps=[WorkflowStep.model_validate(step) for step in steps],
        parameters=parameters or {},
        enabled=enabled,
    )


# ------------------------------------------------------- dependency graph (§48)


def test_find_cycle_detects_loop():
    steps = [
        {"step_id": "sa", "tool": "t", "depends_on": ["sb"]},
        {"step_id": "sb", "tool": "t", "depends_on": ["sa"]},
    ]
    parsed = [WorkflowStep.model_validate(step) for step in steps]
    cycle = find_cycle(parsed)
    assert set(cycle) == {"sa", "sb"}
    assert topological_order(parsed) is None


def test_topological_order_respects_dependencies():
    parsed = [
        WorkflowStep.model_validate({"step_id": "late", "tool": "t", "depends_on": ["first"]}),
        WorkflowStep.model_validate({"step_id": "first", "tool": "t"}),
        WorkflowStep.model_validate({"step_id": "third", "tool": "t", "depends_on": ["late"]}),
    ]
    ordered = topological_order(parsed)
    assert [step.step_id for step in ordered] == ["first", "late", "third"]


def test_create_rejects_dependency_cycle():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp))
        result = asyncio.run(engine.create(definition([
            {"step_id": "s1", "tool": "probe_a", "depends_on": ["s2"]},
            {"step_id": "s2", "tool": "probe_a", "depends_on": ["s1"]},
        ])))
        assert result["success"] is False
        assert any("ciclo" in problem.lower() for problem in result["problems"])


# ------------------------------------------------------------ output binding (§49)


def test_substitute_outputs_walks_dotted_paths():
    outputs = {
        "s1": {"success": True, "data": {"path": "C:/temp/x.txt", "nested": {"id": 42}}},
    }
    params = {"file": "{s1.output.data.path}", "ident": "{s1.output.data.nested.id}"}
    resolved = substitute_outputs(params, outputs)
    assert resolved["file"] == "C:/temp/x.txt"
    assert resolved["ident"] == "42"


def test_substitute_outputs_keeps_unknown_placeholder():
    resolved = substitute_outputs({"x": "{ghost.output.field}"}, {})
    assert resolved["x"] == "{ghost.output.field}"


# ------------------------------------------------------------------ preflight (§51)


def test_preflight_reports_tools_params_and_approvals():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp), registry=FakeRegistry())
        engine._workflows["wf_pf"] = definition([
            {"step_id": "read_step", "tool": "probe_a"},
            {"step_id": "danger", "tool": "mutator_b"},
        ], parameters={"required": ""})
        report = engine.preflight("wf_pf")
        assert report["success"] is True
        assert report["tools_available"] is True
        assert "danger" in report["approvals_expected"]
        assert report["missing_parameters"] == []
        assert report["execution_order"] == ["read_step", "danger"]
        assert report["ready_to_run"] is True


def test_preflight_flags_missing_parameter():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp))
        engine._workflows["wf_pf2"] = definition([
            {"step_id": "s1", "tool": "probe_a", "params": {"path": "{target_path}"}},
        ], parameters={"target_path": ""})
        empty = engine.preflight("wf_pf2", {"target_path": ""})
        filled = engine.preflight("wf_pf2", {"target_path": "C:/x"})
        assert empty["missing_parameters"] == ["target_path"]
        assert empty["ready_to_run"] is False
        assert filled["ready_to_run"] is True


# ------------------------------------------------------------------- dry run (§52)


def test_dry_run_shows_hardening_plan_without_executing():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp))
        engine._workflows["wf_dr"] = definition([
            {"step_id": "s1", "tool": "probe_a",
             "verification_probe": {"tool": "probe_a", "expect_contains": "ok"},
             "timeout_seconds": 30,
             "retry_policy": {"max_retries": 2},
             "rollback": {"tool": "rollback_c"}},
        ])
        plan = engine.dry_run("wf_dr")
        assert plan["success"] is True
        step_plan = plan["plan"][0]
        assert step_plan["verification_probe"] is True
        assert step_plan["timeout_seconds"] == 30
        assert step_plan["max_retries"] == 2
        assert step_plan["rollback"] is True


# ------------------------------------------------------------------ run (§46-§57)


def test_run_success_with_verification_probe_marks_verified(tmp_path):
    registry = FakeRegistry()
    registry.set("probe_a", [FakeResult(True, {"path": "C:/tmp/a.txt"})])
    registry.set("verifier", [FakeResult(True, {"stdout": "path=C:/tmp/a.txt"})])
    engine = make_engine(tmp_path, registry)
    asyncio.run(engine.create(definition([
        {"step_id": "write_step", "tool": "probe_a",
         "verification_probe": {"tool": "verifier", "expect_contains": "c:/tmp/a.txt"}},
    ], workflow_id="wf_verify")))
    result = asyncio.run(engine.run("wf_verify"))
    assert result["success"] is True, result
    assert result["state"] == RUN_STATE_SUCCEEDED
    assert result["executed_steps"][0]["status"] == STEP_VERIFIED
    assert result["executed_steps"][0]["retries_used"] == 0


def test_run_fails_fast_on_missing_parameters_before_any_execution(tmp_path):
    registry = FakeRegistry()
    engine = make_engine(tmp_path, registry)
    asyncio.run(engine.create(definition([
        {"step_id": "s1", "tool": "probe_a", "params": {"path": "{target}"}},
    ], parameters={"target": ""}, workflow_id="wf_param")))
    result = asyncio.run(engine.run("wf_param"))
    assert result["success"] is False
    assert result["error_code"] == "MISSING_PARAMETERS"
    assert result["executed_steps"] == []  # nada executou antes da validação (§50)
    assert registry.calls == []


def test_run_timeout_isolated_per_step(tmp_path):
    class SlowRegistry(FakeRegistry):
        async def execute(self, name, payload, *, exposure="internal"):
            assert exposure in {"internal", "llm", "api"}
            self.calls.append((name, dict(payload)))
            await asyncio.sleep(5)
            return FakeResult(True)

    registry = SlowRegistry()
    engine = make_engine(tmp_path, registry)
    asyncio.run(engine.create(definition([
        {"step_id": "slow_step", "tool": "probe_a", "timeout_seconds": 1,
         "retry_policy": {"max_retries": 0}},
    ], workflow_id="wf_timeout")))
    result = asyncio.run(engine.run("wf_timeout"))
    assert result["success"] is False
    assert result["error_code"] == "STEP_TIMEOUT"
    assert result["executed_steps"][0]["status"] == "FAILED"


def test_retry_only_for_transient_failures(tmp_path):
    registry = FakeRegistry()
    flaky = FakeResult(False, {"message": "connection refused by peer"})
    permanent = FakeResult(False, {"message": "validation rejected payload"})
    registry.set("probe_a", [flaky, flaky, permanent])
    engine = make_engine(tmp_path, registry)
    asyncio.run(engine.create(definition([
        {"step_id": "retry_step", "tool": "probe_a",
         "retry_policy": {"max_retries": 2, "backoff_seconds": 0}},
    ], workflow_id="wf_retry")))
    result = asyncio.run(engine.run("wf_retry"))
    assert result["state"] == RUN_STATE_FAILED
    step = result["executed_steps"][0]
    assert step["attempt"] == 3
    assert step["retries_used"] == 2


def test_rollback_invoked_after_failure(tmp_path):
    registry = FakeRegistry()
    registry.set("mutator_b", [FakeResult(False, {"message": "boom"})])
    engine = make_engine(tmp_path, registry)
    asyncio.run(engine.create(definition([
        {"step_id": "danger_step", "tool": "mutator_b",
         "rollback": {"tool": "rollback_c", "params": {"undo": True}}},
    ], workflow_id="wf_rollb")))
    result = asyncio.run(engine.run("wf_rollb"))
    executed = {name for name, _payload in registry.calls}
    assert "rollback_c" in executed  # compensação executada (§55)
    step = result["executed_steps"][0]
    assert step.get("rollback_status") is True


def test_approval_required_pauses_then_resume_skips_verified(tmp_path):
    registry = FakeRegistry()
    approval_payload = {"error_code": "APPROVAL_REQUIRED", "approval_id": "ap_1"}
    registry.set("mutator_b", [
        FakeResult(False, approval_payload),          # primeira tentativa pausa
        FakeResult(True, {"effect_verified": True}),  # pós-aprovação
    ])
    engine = make_engine(tmp_path, registry)
    engine.approval_lookup = lambda approval_id: SimpleNamespace(status="GRANTED") \
        if approval_id == "ap_1" else None
    asyncio.run(engine.create(definition([
        {"step_id": "gate_step", "tool": "mutator_b"},
        {"step_id": "after_gate", "tool": "probe_a"},
    ], workflow_id="wf_gate")))
    first = asyncio.run(engine.run("wf_gate"))
    assert first["state"] == RUN_STATE_WAITING_USER, first
    assert first["error_code"] == "APPROVAL_REQUIRED"

    resumed = asyncio.run(engine.resume(first["run_id"]))
    assert resumed["success"] is True, resumed
    statuses = [(item["step_id"], item["status"]) for item in resumed["executed_steps"]]
    assert ("gate_step", STEP_VERIFIED) in statuses \
        or ("gate_step", "SUCCEEDED") in statuses
    gate_entry = next(item for item in resumed["executed_steps"] if item["step_id"] == "gate_step")
    assert gate_entry.get("approval_id") == "ap_1"  # re-executado com aprovação injetada


def test_resume_without_grant_stays_waiting(tmp_path):
    registry = FakeRegistry()
    registry.set("mutator_b", [FakeResult(False, {"error_code": "APPROVAL_REQUIRED",
                                                  "approval_id": "ap_9"})])
    engine = make_engine(tmp_path, registry)
    engine.approval_lookup = lambda _approval_id: SimpleNamespace(status="PENDING")
    asyncio.run(engine.create(definition([
        {"step_id": "gated", "tool": "mutator_b"},
    ], workflow_id="wf_wait")))
    first = asyncio.run(engine.run("wf_wait"))
    assert first["state"] == RUN_STATE_WAITING_USER
    again = asyncio.run(engine.resume(first["run_id"]))
    assert again["success"] is False
    assert again["error_code"] == "APPROVAL_REQUIRED"


def test_double_run_same_workflow_is_blocked(tmp_path):
    registry = FakeRegistry()

    class BlockingRegistry(FakeRegistry):
        def __init__(self):
            super().__init__()
            self.gate = asyncio.Event()

        async def execute(self, name, payload, *, exposure="internal"):
            assert exposure in {"internal", "llm", "api"}
            await self.gate.wait()
            return FakeResult(True)

    blocking = BlockingRegistry()
    engine = make_engine(tmp_path, blocking)
    asyncio.run(engine.create(definition([{"step_id": "s1", "tool": "probe_a"}],
                                          workflow_id="wf_lock")))

    async def scenario():
        task_first = asyncio.create_task(engine.run("wf_lock"))
        await asyncio.sleep(0.05)
        second = await engine.run("wf_lock")
        blocking.gate.set()
        first = await task_first
        return first, second

    first, second = asyncio.run(scenario())
    assert first["success"] is True
    assert second["success"] is False
    assert second["error_code"] == "WORKFLOW_ALREADY_RUNNING"  # §24/§26


# ------------------------------------------------------------------ history (§58)


def test_history_persisted_and_queryable(tmp_path):
    registry = FakeRegistry()
    engine = make_engine(tmp_path, registry)
    asyncio.run(engine.create(definition([{"step_id": "s1", "tool": "probe_a"}],
                                          workflow_id="wf_hist")))
    result = asyncio.run(engine.run("wf_hist"))
    listing = asyncio.run(engine.history())
    assert listing["count"] >= 1
    record = asyncio.run(engine.history(result["run_id"]))
    stored = record["runs"][0]
    assert stored["workflow_id"] == "wf_hist"
    assert stored["started_at"] and stored["finished_at"]
    assert stored["steps"][0]["status"] in {"SUCCEEDED", "VERIFIED"}


# ------------------------------------------------------------------ templates (§59+)


def test_seed_templates_idempotent_preserves_operator_edits():
    import tempfile
    from pathlib import Path

    repo_templates = Path(__file__).resolve().parents[2] / "config" / "workflow_templates.json"
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp), registry=None)  # sem registry: validação de tools é pulada
        seeded = engine.seed_templates(repo_templates)
        assert seeded["success"] is True
        assert "wf_check_kazumi_health" in seeded["created"]
        assert len(seeded["created"]) >= 7  # §59 mínimo de 7 templates
        # operador edita um template
        current = engine.get("wf_check_homelab")
        updated = current.model_dump()
        updated["name"] = "Meu Homelab Editado"
        asyncio.run(engine.update("wf_check_homelab", {"name": updated["name"]}))
        # segundo seed NÃO sobrescreve (idempotente)
        again = engine.seed_templates(repo_templates)
        assert again["created"] == []
        assert engine.get("wf_check_homelab").name == "Meu Homelab Editado"


# --------------------------------------------------------------- health matrix (§11+)


@pytest.mark.asyncio
async def test_build_health_report_shape_and_verdict():
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app import main as app_main

    with TestClient(app_main.app) as client:
        services = app_main.app.state.services
        services.llm.health = AsyncMock(return_value=True)
        services.llm.ready = AsyncMock(return_value=True)
        report = await _report(services)
        assert report["overall"] in {"READY", "DEGRADED", "FAILED"}
        assert {"llm", "memory", "database", "voice", "tools", "watchdog"} <= set(report["subsystems"])
        for entry in report["subsystems"].values():
            assert entry["state"] in {"DISABLED", "UNCONFIGURED", "STARTING", "READY",
                                      "DEGRADED", "FAILED", "OFFLINE", "RECOVERING", "STALE"}
            assert entry["observed_at"]
        nodes = {node["id"] for node in report["graph"]["nodes"]}
        edges = report["graph"]["edges"]
        assert all(edge["from"] in nodes and edge["to"] in nodes for edge in edges)
        assert client.get("/api/health_report").status_code == 200


async def _report(services):
    from app.core.health_matrix import build_health_report

    return await build_health_report(services)


# ---------------------------------------------------------------- error envelope (§18)


def test_unhandled_exception_returns_safe_envelope():
    from fastapi.testclient import TestClient

    from app import main as app_main
    from app.core.errors import safe_envelope, unhandled_exception_handler

    envelope = safe_envelope(RuntimeError("stack with token abc123"))
    assert set(envelope) == {"error_code", "safe_message", "stage", "recoverable"}
    assert "abc123" not in envelope["safe_message"]

    class DummyRequest:
        url = SimpleNamespace(path="/x")
        method = "GET"

    response = asyncio.run(unhandled_exception_handler(DummyRequest(), ValueError("boom")))
    assert response.status_code == 500
    assert set(json.loads(response.body)) == {"error_code", "safe_message", "stage", "recoverable"}

    with TestClient(app_main.app, raise_server_exceptions=False) as client:
        from unittest.mock import AsyncMock

        original = app_main.app.state.services.memory.health

        async def boom():
            raise RuntimeError("segredo super secreto")

        app_main.app.state.services.memory.health = boom
        try:
            response = client.get("/api/health")
        finally:
            app_main.app.state.services.memory.health = original
        assert response.status_code == 500
        body = response.json()
        assert body["error_code"] == "INTERNAL_ERROR"
        assert "segredo" not in json.dumps(body)


# ------------------------------------------------------------------ daily check (§240+)


def test_daily_check_categories_and_fixture_proof():
    from fastapi.testclient import TestClient

    from app.core.daily_check import FAIL, DEGRADED, PASS, SKIPPED

    with TestClient(app_main_app()) as client:
        report = client.post("/api/daily_check/run").json()
        expected = {"Conversation", "LLM", "Voice", "Desktop", "Browser", "Filesystem",
                    "Runtime", "Jobs", "Workflows", "Watchdog", "Homelab", "Integrations"}
        assert expected <= set(report["categories"])
        valid = {PASS, DEGRADED, FAIL, SKIPPED}
        for category in report["categories"].values():
            assert category["result"] in valid
        filesystem = report["categories"]["Filesystem"]
        assert filesystem["result"] == PASS, filesystem
        assert "delete" in filesystem["details"]["steps_completed"]
        browser = report["categories"]["Browser"]
        assert browser["result"] == SKIPPED
        history = client.get("/api/daily_check/history").json()
        assert history["count"] >= 1


def app_main_app():
    from app import main as app_main

    return app_main.app


# ---------------------------------------------------------------- benchmark lab (K-Q)


def _make_lab(monkeypatch_tmp=None, installed=("qwen3:8b",)):
    from app.benchmark.lab import ModelBenchmarkLab, ModelNotInstalled

    lab = ModelBenchmarkLab("http://127.0.0.1:11434")

    async def fake_installed():
        return [{"name": name} for name in installed]

    lab.installed_models = fake_installed
    return lab, ModelNotInstalled


def test_benchmark_model_not_installed_is_valid_state():
    lab, ModelNotInstalled = _make_lab(installed=("qwen3:8b",))

    async def scenario():
        with pytest.raises(ModelNotInstalled):
            await lab.perf_run("qwen3:14b")
        profile = await lab.resolve_profile("qwen-14b-candidate")
        overview = await lab.profiles_overview()
        return profile, overview

    profile, overview = asyncio.run(scenario())
    assert profile["installed"] is False
    assert profile["display_state"] == "NOT INSTALLED"  # §99
    candidate = overview["candidates"][0]
    assert candidate["profile_id"] == "qwen-14b-candidate"
    assert candidate["installed"] is False
    assert candidate["benchmark_ready"] is True  # ausência não é erro (§100)


def test_benchmark_background_run_records_model_not_installed():
    from app.benchmark.lab import BenchmarkRunRegistry, ModelBenchmarkLab

    lab = ModelBenchmarkLab("http://127.0.0.1:11434", registry=BenchmarkRunRegistry())

    async def fake_installed():
        return [{"name": "qwen3:8b"}]

    lab.installed_models = fake_installed

    async def scenario():
        started = lab.start_run("perf", model_id="nao-instalado:99b")

        async def wait_done():
            for _ in range(50):
                entry = lab.registry.get(started["run_id"])
                if entry["state"] in {"DONE", "FAILED"}:
                    return entry
                await asyncio.sleep(0.05)
            raise AssertionError("run never finished")

        return await wait_done()

    entry = asyncio.run(scenario())
    assert entry["state"] == "FAILED"
    assert entry["error_code"] == "MODEL_NOT_INSTALLED"  # §69/§288


def test_quality_scoring_is_deterministic():
    from app.benchmark.lab import QUALITY_CASES, _extract_json_steps, _score_case

    case_tool = next(case for case in QUALITY_CASES if case["case_id"] == "tool_ping")
    score, checks = _score_case(case_tool, "", [{"function": {"name": "ping_host"}}])
    assert score == 1.0 and checks["expected_tool_chosen"]

    score_wrong, _checks = _score_case(case_tool, "", [{"function": {"name": "desktop_open_application"}}])
    assert score_wrong == 0.0  # ferramenta errada zera (§91)

    grounding = next(case for case in QUALITY_CASES if case["case_id"] == "grounding_empty_result")
    honest = "Não encontrei nenhuma janela do Notepad; não posso afirmar que tenha sido iniciado."
    lying = "O bloco de notas está aberto com pid 4242."
    score_honest, _ = _score_case(grounding, honest, [])
    score_lying, checks_lying = _score_case(grounding, lying, [])
    assert score_honest == 1.0
    assert score_lying == 0.0 and checks_lying["no_forbidden_content"] is False  # §93

    steps_case = next(case for case in QUALITY_CASES if case["case_id"] == "multi_notepad")
    good_json = 'Aqui está:\n[{"tool":"desktop_open_application","arguments":{}},'\
                '{"tool":"ui_send_keys","arguments":{}},{"tool":"system_shell","arguments":{}}]'
    score_steps, checks_steps = _score_case(steps_case, good_json, [])
    assert score_steps == 1.0 and checks_steps["enough_steps"]
    assert _extract_json_steps("sem json aqui") is None


def test_compare_promotion_gate_manual_only():
    from app.benchmark.lab import ModelBenchmarkLab, extract_metrics

    lab = ModelBenchmarkLab("http://127.0.0.1:11434")
    base_doc = {
        "model_id": "current", "vram_bytes_loaded": 6_000_000_000,
        "perf": {"summary": {"ttft_ms_median_warm": 300},
                 "contexts": {"2048": {"warm_median": {"ttft_ms": 280}}}},
        "quality": {"totals": {"tool_accuracy": 0.75, "grounding_score": 1.0,
                               "multi_step_score": 0.5, "recovery_score": 1.0}},
    }
    cand_better = {
        "model_id": "candidate", "vram_bytes_loaded": 12_000_000_000,
        "perf": {"summary": {"ttft_ms_median_warm": 400},
                 "contexts": {"2048": {"warm_median": {"ttft_ms": 380}}}},
        "quality": {"totals": {"tool_accuracy": 0.9, "grounding_score": 1.0,
                               "multi_step_score": 0.75, "recovery_score": 1.0}},
    }
    cand_worse = {**cand_better, "quality": {"totals": {"tool_accuracy": 0.5,
                                                        "grounding_score": 0.5,
                                                        "multi_step_score": 0.25,
                                                        "recovery_score": 0.5}}}

    def fake_loader(label):
        return {"base": base_doc, "good": cand_better, "bad": cand_worse}.get(label)

    lab.load_baseline_document = fake_loader

    good = lab.compare("base", "good")
    assert good["success"] is True
    assert good["all_passed"] is True
    assert good["promotion"] == "MANUAL_ONLY"  # §104/§106

    bad = lab.compare("base", "bad")
    assert bad["all_passed"] is False
    assert "manter" in bad["recommendation"].lower()

    missing = lab.compare("base", "fantasma")
    assert missing["success"] is False and missing["error_code"] == "BASELINE_NOT_FOUND"

    metrics = extract_metrics({**base_doc})
    assert metrics["tool_accuracy"] == 0.75
    assert metrics["ttft_ms"] == 280


def test_extract_metrics_handles_empty_document():
    from app.benchmark.lab import extract_metrics

    metrics = extract_metrics({})
    assert metrics["tool_accuracy"] is None
    assert metrics["ttft_ms"] is None
