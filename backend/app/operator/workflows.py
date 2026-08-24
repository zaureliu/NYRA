"""Human Workflow Memory + Production Workflow Engine (spec Parte F-J, BB).

Structured executable procedures — NOT conversational memory.

Hardening contract implemented here (prompt10 §45-§58, §234-§239):

* step model: step_id / tool / arguments(params) / dependencies / risk /
  verification probe / retry_policy / rollback / timeout / status
* dependency graph with cycle validation (§48)
* output binding: ``{step_id.output.field.sub}`` (§49)
* parameter validation BEFORE any execution (§50)
* preflight: tools available, parameters resolved, approvals expected (§51)
* dry run: plan without executing (§52)
* resume skipping already VERIFIED/SUCCEEDED steps (§53-§54)
* rollback per step when declared (§55)
* retry per step, bounded, transient-only, configurable (§56)
* WAITING_FOR_USER on approval-required results (§57)
* persisted history: started/steps/results/verification/finish (§58)
* resource lock per workflow_id — no concurrent double-run (§24/§26)

Approvals and grounding keep applying at execution time because every step is
a normal ToolRegistry call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.paths import DATA_ROOT, PROJECT_ROOT
from app.tools.redaction import redact_secrets

logger = logging.getLogger("nyra.operator.workflows")

RUN_STATE_RUNNING = "RUNNING"
RUN_STATE_SUCCEEDED = "SUCCEEDED"
RUN_STATE_FAILED = "FAILED"
RUN_STATE_CANCELLED = "CANCELLED"
RUN_STATE_WAITING_USER = "WAITING_FOR_USER"

STEP_PENDING = "PENDING"
STEP_RUNNING = "RUNNING"
STEP_SUCCEEDED = "SUCCEEDED"
STEP_VERIFIED = "VERIFIED"
STEP_FAILED = "FAILED"
STEP_SKIPPED = "SKIPPED"
STEP_WAITING_USER = "WAITING_FOR_USER"
STEP_ROLLED_BACK = "ROLLED_BACK"

_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection", "unreachable", "temporarily",
    "refused", "reset by peer", "overloaded", "503", "502", "504",
)

DEFAULT_STEP_TIMEOUT_SECONDS = 120.0
MAX_STEP_TIMEOUT_SECONDS = 600.0


class RetryPolicy(BaseModel):
    max_retries: int = Field(default=0, ge=0, le=3)
    backoff_seconds: float = Field(default=1.0, ge=0.0, le=30.0)


class VerificationProbe(BaseModel):
    """Deterministic post-step verification (§32): a read-only tool call whose
    output must contain ``expect_contains`` to mark the step VERIFIED."""

    tool: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    expect_contains: str = Field(default="", max_length=200)


class RollbackSpec(BaseModel):
    """Compensating action executed when the step fails after mutating (§55)."""

    tool: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    step_id: str = Field(min_length=2, max_length=60)
    tool: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    verification: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=200)
    # ---- hardening extensions (all optional; old definitions stay valid) ----
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=MAX_STEP_TIMEOUT_SECONDS)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    verification_probe: VerificationProbe | None = None
    rollback: RollbackSpec | None = None
    risk_override: str | None = Field(
        default=None, pattern=r"^(READ_ONLY|LOW_RISK|ELEVATED|DESTRUCTIVE|CRITICAL)$")


class WorkflowDefinition(BaseModel):
    workflow_id: str = Field(pattern=r"^wf_[a-z0-9_]{3,48}$")
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=400)
    trigger_phrases: list[str] = Field(default_factory=list, max_length=10)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=20)
    parameters: dict[str, str] = Field(default_factory=dict)
    risk: str = Field(default="LOW_RISK", pattern=r"^(READ_ONLY|LOW_RISK|ELEVATED|DESTRUCTIVE|CRITICAL)$")
    version: int = Field(default=1, ge=1, le=999)
    enabled: bool = True


class WorkflowValidationError(Exception):
    pass


_PARAM_RE = re.compile(r"\{([a-z_][a-z0-9_]{0,30})\}")
_OUTPUT_RE = re.compile(r"\{([A-Za-z0-9_]+)\.output\.([A-Za-z0-9_.]+)\}")

_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    error_code TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    parameters TEXT,
    steps TEXT,
    duration_ms REAL
)
"""


def substitute_parameters(value: Any, bindings: dict[str, str]) -> Any:
    """Replace {name} placeholders inside string leaves (§201)."""
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key in bindings:
                return str(bindings[key])[:500]
            return match.group(0)

        return _PARAM_RE.sub(replace, value)
    if isinstance(value, dict):
        return {key: substitute_parameters(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_parameters(item, bindings) for item in value]
    return value


def substitute_outputs(value: Any, outputs: dict[str, Any]) -> Any:
    """Replace {stepX.output.path} placeholders from previous step data (§49)."""
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            step_id, dotted = match.group(1), match.group(2)
            payload = outputs.get(step_id)
            if payload is None:
                return match.group(0)
            current: Any = payload
            for part in dotted.split("."):
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return match.group(0)
            return str(current)[:500]

        return _OUTPUT_RE.sub(replace, value)
    if isinstance(value, dict):
        return {key: substitute_outputs(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_outputs(item, outputs) for item in value]
    return value


def topological_order(steps: list[WorkflowStep]) -> list[WorkflowStep] | None:
    """Kahn topological sort preserving declaration order; None on cycle (§48)."""
    by_id = {step.step_id: index for index, step in enumerate(steps)}
    incoming = {step.step_id: 0 for step in steps}
    outgoing: dict[str, list[str]] = {step.step_id: [] for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            if dependency in by_id:
                incoming[step.step_id] += 1
                outgoing[dependency].append(step.step_id)
    ready = [step for step in steps if incoming[step.step_id] == 0]
    ordered: list[WorkflowStep] = []
    while ready:
        ready.sort(key=lambda item: by_id[item.step_id])
        current = ready.pop(0)
        ordered.append(current)
        for follower in outgoing[current.step_id]:
            incoming[follower] -= 1
            if incoming[follower] == 0:
                ready.append(next(item for item in steps if item.step_id == follower))
    if len(ordered) != len(steps):
        return None
    return ordered


def find_cycle(steps: list[WorkflowStep]) -> list[str]:
    """Returns the ids participating in a dependency cycle (empty if none)."""
    remaining = {step.step_id: set(step.depends_on) & {s.step_id for s in steps} for step in steps}
    resolved: set[str] = set()
    changed = True
    while changed and remaining:
        changed = False
        for step_id, deps in list(remaining.items()):
            if deps <= resolved:
                resolved.add(step_id)
                remaining.pop(step_id)
                changed = True
    return sorted(remaining)


def _is_transient(message: str) -> bool:
    lowered = message.casefold()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _step_summary(data: Any) -> str:
    if not isinstance(data, dict):
        return redact_secrets(str(data)[:160])
    message = data.get("message") or data.get("error_code") or ("ok" if data.get("success") else "sem detalhe")
    verified = data.get("effect_verified")
    suffix = f" | effect_verified={verified}" if verified is not None else ""
    return f"{str(message)[:140]}{suffix}"


class WorkflowRunStore:
    """SQLite persistence of workflow run history (§58)."""

    def __init__(self, database_path=None) -> None:
        self.database_path = Path(database_path or (DATA_ROOT / "nyra.db"))
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        import aiosqlite

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(_RUNS_TABLE)
            await db.commit()
        self._initialized = True

    async def save(self, record: dict) -> None:
        import aiosqlite

        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(_RUNS_TABLE)
            await db.execute(
                "INSERT OR REPLACE INTO workflow_runs "
                "(run_id, workflow_id, version, state, error_code, started_at, finished_at,"
                " parameters, steps, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    record["run_id"], record["workflow_id"], int(record.get("version", 1)),
                    record["state"], record.get("error_code"), record["started_at"],
                    record.get("finished_at"),
                    json.dumps(record.get("parameters") or {}, ensure_ascii=False),
                    json.dumps(record.get("steps") or [], ensure_ascii=False),
                    record.get("duration_ms"),
                ),
            )
            await db.commit()

    async def get(self, run_id: str) -> dict | None:
        import aiosqlite

        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        record = dict(row)
        record["parameters"] = json.loads(record.get("parameters") or "{}")
        record["steps"] = json.loads(record.get("steps") or "[]")
        return record

    async def list(self, *, limit: int = 25) -> list[dict]:
        import aiosqlite

        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?", (int(limit),))
            rows = await cursor.fetchall()
        items = []
        for row in rows:
            record = dict(row)
            record["parameters"] = {}
            try:
                record["steps"] = json.loads(record.get("steps") or "[]")
            except ValueError:
                record["steps"] = []
            items.append(record)
        return items


class WorkflowEngine:
    def __init__(self, registry=None, event_bus=None, *,
                 store_path=None, history_store: WorkflowRunStore | None = None) -> None:
        self.registry = registry
        self.event_bus = event_bus
        self.store_path = store_path or (DATA_ROOT / "workflows.json")
        self.history_store = history_store or WorkflowRunStore()
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._active_runs: dict[str, str] = {}  # workflow_id -> run_id (§24)
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        try:
            if not self.store_path.exists():
                return
            document = json.loads(self.store_path.read_text("utf-8"))
            for entry in document.get("workflows", []):
                workflow = WorkflowDefinition.model_validate(entry)
                self._workflows[workflow.workflow_id] = workflow
        except (OSError, ValueError) as exc:
            logger.warning("workflow store unreadable: %s", exc)

    def _save(self) -> None:
        payload = {"version": 1,
                   "workflows": [item.model_dump(mode="json") for item in self._workflows.values()]}
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.store_path)

    # --------------------------------------------------------------------- CRUD
    async def create(self, definition: WorkflowDefinition) -> dict:
        problems = await self._validate_steps(definition)
        if problems:
            return {"success": False, "error_code": "VALIDATION_FAILED",
                    "problems": problems}
        if definition.workflow_id in self._workflows:
            return {"success": False, "error_code": "WORKFLOW_EXISTS"}
        self._workflows[definition.workflow_id] = definition
        self._save()
        return {"success": True, "workflow": definition.model_dump()}

    async def update(self, workflow_id: str, updates: dict[str, Any]) -> dict:
        current = self._workflows.get(workflow_id)
        if not current:
            return {"success": False, "error_code": "WORKFLOW_NOT_FOUND"}
        merged_data = current.model_dump()
        allowed = ("name", "description", "trigger_phrases", "steps", "parameters",
                   "risk", "enabled")
        changed = False
        for key in allowed:
            if key in updates:
                merged_data[key] = updates[key]
                changed = True
        if not changed:
            return {"success": False, "error_code": "NO_CHANGES"}
        merged_data["version"] = int(merged_data.get("version", 1)) + 1  # §198
        updated = WorkflowDefinition.model_validate(merged_data)
        problems = await self._validate_steps(updated)
        if problems:
            return {"success": False, "error_code": "VALIDATION_FAILED", "problems": problems}
        self._workflows[workflow_id] = updated
        self._save()
        return {"success": True, "workflow": updated.model_dump()}

    async def delete(self, workflow_id: str) -> dict:
        removed = self._workflows.pop(workflow_id, None)
        if removed is None:
            return {"success": False, "error_code": "WORKFLOW_NOT_FOUND"}
        self._save()
        return {"success": True, "deleted": workflow_id}

    def get(self, workflow_id: str) -> WorkflowDefinition | None:
        return self._workflows.get(workflow_id)

    def list_workflows(self) -> dict:
        items = []
        for item in self._workflows.values():
            data = item.model_dump()
            data["step_count"] = len(item.steps)
            items.append(data)
        return {"success": True, "workflows": items, "count": len(items)}

    def find_by_trigger(self, text: str) -> WorkflowDefinition | None:
        clean = (text or "").strip().casefold()
        if not clean:
            return None
        for item in self._workflows.values():
            if not item.enabled:
                continue
            for phrase in item.trigger_phrases:
                phrase_clean = phrase.strip().casefold()
                if phrase_clean and phrase_clean in clean:
                    return item
        return None

    # ------------------------------------------------------------------ templates
    def seed_templates(self, templates_path=None) -> dict:
        """Idempotent seeding of official templates (spec §59-§62).

        Missing template ids are created; existing definitions — including
        operator edits — are never overwritten. Returns a summary.
        """
        path = Path(templates_path or (PROJECT_ROOT / "config" / "workflow_templates.json"))
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return {"success": False, "error_code": "TEMPLATES_UNREADABLE",
                    "message": str(error)[:160]}
        created, skipped, invalid = [], [], []
        for entry in document.get("templates", []):
            try:
                definition = WorkflowDefinition.model_validate(entry)
            except Exception:  # noqa: BLE001 - pydantic ValidationError
                invalid.append(str(entry.get("workflow_id", "?")))
                continue
            if definition.workflow_id in self._workflows:
                skipped.append(definition.workflow_id)
                continue
            self._workflows[definition.workflow_id] = definition
            created.append(definition.workflow_id)
        if created:
            try:
                self._save()
            except OSError as error:
                return {"success": False, "error_code": "TEMPLATES_SAVE_FAILED",
                        "message": str(error)[:160], "created": created}
        return {"success": True, "created": created, "skipped": skipped,
                "invalid": invalid}

    # ---------------------------------------------------------------- validation
    async def _validate_steps(self, definition: WorkflowDefinition) -> list[str]:
        problems: list[str] = []
        seen_ids: set[str] = set()
        known_tools = set()
        if self.registry is not None:
            for descriptor in self.registry.descriptions():
                name = descriptor.get("name") if isinstance(descriptor, dict) else getattr(descriptor, "name", None)
                if name:
                    known_tools.add(name)
        for step in definition.steps:
            if step.step_id in seen_ids:
                problems.append(f"step_id duplicado: {step.step_id}")
            seen_ids.add(step.step_id)
            for dependency in step.depends_on:
                if dependency not in seen_ids and all(item.step_id != dependency for item in definition.steps):
                    problems.append(f"'{step.step_id}' depende de step inexistente '{dependency}'")
            if known_tools and step.tool not in known_tools:
                problems.append(f"tool desconhecida no registry: '{step.tool}' (§199)")
        cycle = find_cycle(definition.steps)
        if cycle:
            problems.append(f"ciclo de dependências detectado envolvendo: {cycle} (§48)")
        return problems

    def missing_parameters(self, definition: WorkflowDefinition,
                           parameters: dict[str, str] | None = None) -> list[str]:
        bindings = {**definition.parameters, **(parameters or {})}
        missing: set[str] = set()
        for step in definition.steps:
            serialized = json.dumps(step.params, ensure_ascii=False)
            for name in _PARAM_RE.findall(serialized):
                value = bindings.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    missing.add(name)
        return sorted(missing)

    # ---------------------------------------------------------------- preflight
    def preflight(self, workflow_id: str,
                  parameters: dict[str, str] | None = None) -> dict:
        """Static pre-execution analysis without running anything (§51)."""
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            return {"success": False, "error_code": "WORKFLOW_NOT_FOUND"}
        known_tools = set()
        risks: dict[str, str] = {}
        if self.registry is not None:
            for descriptor in self.registry.descriptions():
                name = descriptor.get("name") if isinstance(descriptor, dict) else getattr(descriptor, "name", None)
                if name:
                    known_tools.add(name)
                    risk = descriptor.get("risk") if isinstance(descriptor, dict) \
                        else getattr(descriptor, "risk", "")
                    risks[name] = getattr(risk, "value", str(risk)) if risk else ""
        order = topological_order(workflow.steps)
        approvals_expected: list[str] = []
        unknown_tools: list[str] = []
        for step in workflow.steps:
            if step.tool not in known_tools and known_tools:
                unknown_tools.append(step.tool)
                continue
            effective_risk = step.risk_override or risks.get(step.tool, "")
            if effective_risk in {"ELEVATED", "DESTRUCTIVE", "CRITICAL"} or effective_risk == "DYNAMIC":
                # DYNAMIC tools may require approval depending on arguments (shell).
                approvals_expected.append(step.step_id)
        missing = self.missing_parameters(workflow, parameters)
        cycle = find_cycle(workflow.steps)
        return {
            "success": True,
            "workflow_id": workflow_id,
            "tools_available": not unknown_tools,
            "unknown_tools": unknown_tools,
            "missing_parameters": missing,
            "cycle": cycle,
            "execution_order": [step.step_id for step in order] if order else [],
            "approvals_expected": approvals_expected,
            "ready_to_run": bool(order and not unknown_tools and not missing and not cycle),
            "estimated_max_duration_seconds": round(sum(
                (step.timeout_seconds or DEFAULT_STEP_TIMEOUT_SECONDS) *
                (1 + step.retry_policy.max_retries) for step in workflow.steps), 1),
        }

    # ------------------------------------------------------------------ dry run
    def dry_run(self, workflow_id: str, parameters: dict[str, str] | None = None) -> dict:
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            return {"success": False, "error_code": "WORKFLOW_NOT_FOUND"}
        bindings = {**workflow.parameters, **(parameters or {})}
        order = topological_order(workflow.steps) or workflow.steps
        plan = []
        missing_params: set[str] = set()
        for index, step in enumerate(order):
            resolved = substitute_parameters(step.params, bindings)
            placeholders = set()
            for value in json.dumps(resolved, ensure_ascii=False).split('"'):
                placeholders.update(_PARAM_RE.findall(str(value)))
            missing_params.update(placeholders)
            plan.append({
                "order": index + 1,
                "step_id": step.step_id,
                "tool": step.tool,
                "params_preview": redact_secrets(json.dumps(resolved, ensure_ascii=False)[:300]),
                "depends_on": step.depends_on,
                "verification": step.verification or "",
                "verification_probe": bool(step.verification_probe),
                "timeout_seconds": step.timeout_seconds or DEFAULT_STEP_TIMEOUT_SECONDS,
                "max_retries": step.retry_policy.max_retries,
                "rollback": bool(step.rollback),
            })
        cycle = find_cycle(workflow.steps)
        note = "Nada foi executado (§200)."
        if missing_params:
            note += " Parâmetros ausentes: " + ", ".join(sorted(missing_params))
        if cycle:
            note += f" Ciclo detectado: {cycle}"
        result = {
            "success": True,
            "workflow_id": workflow_id,
            "dry_run": True,
            "plan": plan,
            "missing_parameters": sorted(missing_params),
            "cycle": cycle,
            "risk": workflow.risk,
            "note": note,
        }
        return result

    # ---------------------------------------------------------------------- run
    async def run(self, workflow_id: str, parameters: dict[str, str] | None = None,
                  *, turn_id: str | None = None, agent_run_id: str | None = None) -> dict:
        """Execute through the ToolRegistry with full hardening (§46-§58).

        Steps run in topological order honoring depends_on; failures respect
        per-step retry policy; verification probes promote SUCCEEDED→VERIFIED;
        approval-required results pause the whole run as WAITING_FOR_USER.
        """
        workflow = self._workflows.get(workflow_id)
        if not workflow:
            return {"success": False, "error_code": "WORKFLOW_NOT_FOUND"}
        if not workflow.enabled:
            return {"success": False, "error_code": "WORKFLOW_DISABLED"}
        if self.registry is None:
            return {"success": False, "error_code": "REGISTRY_UNAVAILABLE"}

        lock = self._run_locks.setdefault(workflow_id, asyncio.Lock())
        if lock.locked():  # §26 double-trigger protection
            return {"success": False, "error_code": "WORKFLOW_ALREADY_RUNNING",
                    "message": f"Workflow '{workflow_id}' já possui execução ativa."}
        async with lock:
            return await self._run_locked(workflow, parameters,
                                          turn_id=turn_id, agent_run_id=agent_run_id)

    async def _run_locked(self, workflow: WorkflowDefinition,
                          parameters: dict[str, str] | None,
                          *, turn_id: str | None, agent_run_id: str | None,
                          resumed_from: dict | None = None) -> dict:
        del turn_id, agent_run_id  # reserved for ledger correlation
        # ---- §50 parameter validation BEFORE executing anything -------------
        missing = self.missing_parameters(workflow, parameters)
        if missing:
            return {"success": False, "error_code": "MISSING_PARAMETERS",
                    "message": f"Parâmetros ausentes: {missing}",
                    "executed_steps": []}
        bindings = {**workflow.parameters, **(parameters or {})}
        order = topological_order(workflow.steps)
        if order is None:
            return {"success": False, "error_code": "DEPENDENCY_CYCLE",
                    "message": "Ciclo de dependências entre steps.",
                    "executed_steps": []}

        await self.history_store.initialize()
        run_id = (resumed_from or {}).get("run_id") or f"wfr_{time.time_ns()}"
        if resumed_from is None:
            await self._emit("WORKFLOW_TRIGGERED", workflow_id=workflow.workflow_id, run_id=run_id)
        started = time.time()
        started_iso = (resumed_from or {}).get("started_at") or _utcnow_iso()
        outputs: dict[str, Any] = {}
        executed: list[dict] = []
        prior_steps: dict[str, dict] = {
            item.get("step_id"): item for item in (resumed_from or {}).get("steps", [])
            if isinstance(item, dict) and item.get("step_id")
        }

        run_state = RUN_STATE_RUNNING
        error_code: str | None = None
        approval_retry: dict[str, str] = {
            str(item.get("step_id")): str(item.get("approval_id"))
            for item in (resumed_from or {}).get("steps", [])
            if isinstance(item, dict) and item.get("status") == STEP_WAITING_USER
            and item.get("approval_id")
        }

        for index, step in enumerate(order):
            previous = prior_steps.get(step.step_id)
            if previous and previous.get("status") in {STEP_VERIFIED, STEP_SUCCEEDED, STEP_SKIPPED}:
                # §54: resume never repeats an already completed/verified step.
                executed.append({**previous, "order": index + 1, "reused": True})
                if previous.get("output") is not None:
                    outputs[step.step_id] = previous["output"]
                continue

            unresolved = json.dumps(step.params, ensure_ascii=False)
            leftover_params = {name for name in _PARAM_RE.findall(unresolved)
                               if name not in bindings or not str(bindings.get(name, "")).strip()}
            leftover_outputs = {match[0] for match in _OUTPUT_RE.findall(unresolved)
                                if match[0] not in outputs}
            if leftover_params or leftover_outputs:
                blocked_by = sorted(leftover_params | {f"{item}.output" for item in leftover_outputs})
                run_state, error_code = RUN_STATE_FAILED, "MISSING_PARAMETERS"
                executed.append({
                    "step_id": step.step_id, "tool": step.tool, "order": index + 1,
                    "status": STEP_FAILED, "ok": False,
                    "summary": f"Parâmetros/outputs ausentes: {blocked_by}",
                })
                break

            params = substitute_outputs(substitute_parameters(step.params,
                                                              {**workflow.parameters, **(parameters or {})}),
                                        outputs)
            if step.step_id in approval_retry:
                # Re-execução do step que aguardava aprovação: injeta o
                # approval_id para o gate consumir (fluxo de uso único).
                granted = await self._approval_granted(approval_retry[step.step_id])
                if not granted:
                    run_state, error_code = RUN_STATE_WAITING_USER, "APPROVAL_REQUIRED"
                    executed.append({
                        "step_id": step.step_id, "tool": step.tool, "order": index + 1,
                        "status": STEP_WAITING_USER, "ok": False,
                        "approval_id": approval_retry[step.step_id],
                        "summary": "Aguardando aprovação do operador.",
                    })
                    break
                params = {**params, "approval_id": approval_retry[step.step_id]}
            step_record = await self._execute_step(workflow, step, params, index)
            if step.step_id in approval_retry:
                step_record["approval_id"] = approval_retry[step.step_id]
            if step_record.get("output") is not None:
                outputs[step.step_id] = step_record["output"]
            executed.append(step_record)
            status = step_record.get("status")

            if status is STEP_WAITING_USER:
                run_state, error_code = RUN_STATE_WAITING_USER, "APPROVAL_REQUIRED"
                break
            if status in {STEP_FAILED, STEP_ROLLED_BACK}:
                run_state, error_code = RUN_STATE_FAILED, step_record.get("error_code") or "STEP_FAILED"
                break

        finished_iso = _utcnow_iso()
        if run_state is RUN_STATE_RUNNING:
            run_state = RUN_STATE_SUCCEEDED  # todos os steps concluíram sem interrupção
        duration_ms = round((time.time() - started) * 1000, 1)
        record = {
            "run_id": run_id,
            "workflow_id": workflow.workflow_id,
            "version": workflow.version,
            "state": run_state,
            "error_code": error_code,
            "started_at": started_iso,
            "finished_at": finished_iso if run_state != RUN_STATE_WAITING_USER else None,
            "parameters": dict(parameters or {}),
            "steps": executed,
            "duration_ms": duration_ms,
        }
        try:
            await self.history_store.save(record)
        except Exception as error:  # noqa: BLE001 - history failure can't mask result
            logger.warning("workflow history save failed: %s", error)

        emit_state = {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED",
                      "WAITING_FOR_USER": "WAITING_FOR_USER"}.get(run_state, run_state)
        await self._emit("WORKFLOW_FINISHED", workflow_id=workflow.workflow_id,
                         run_id=run_id, state=emit_state)
        response = {
            "success": run_state == RUN_STATE_SUCCEEDED,
            "run_id": run_id,
            "workflow_id": workflow.workflow_id,
            "state": run_state,
            "executed_steps": executed,
            "duration_ms": duration_ms,
        }
        if error_code:
            response["error_code"] = error_code
        if run_state == RUN_STATE_FAILED:
            response["message"] = "Workflow interrompido com relatório parcial; consulte o histórico."
        if run_state == RUN_STATE_WAITING_USER:
            response["message"] = "Aprovação do operador necessária; use resume após conceder."
        return response

    async def _execute_step(self, workflow: WorkflowDefinition, step: WorkflowStep,
                            params: dict, index: int) -> dict:
        del workflow  # reservado para políticas por workflow
        timeout = min(step.timeout_seconds or DEFAULT_STEP_TIMEOUT_SECONDS,
                      MAX_STEP_TIMEOUT_SECONDS)
        retries_left = step.retry_policy.max_retries
        attempt = 0
        while True:
            attempt += 1
            record = await self._attempt_once(step, params, index, attempt, timeout)
            record.setdefault("retries_used", 0)
            if record["status"] in {STEP_SUCCEEDED, STEP_VERIFIED, STEP_WAITING_USER}:
                return record
            message = record.get("summary", "")
            transient = _is_transient(record.get("error_type", "") + " " + message)
            if retries_left > 0 and transient:  # §35/§36: retry só para falha transitória
                retries_left -= 1
                record["retries_used"] = attempt
                await asyncio.sleep(step.retry_policy.backoff_seconds)
                continue
            record["retries_used"] = attempt - 1
            if step.rollback is not None:
                rolled = await self._run_rollback(step)
                record["rollback_status"] = rolled
                if rolled:
                    record["status"] = STEP_ROLLED_BACK
            return record

    async def _attempt_once(self, step: WorkflowStep, params: dict, index: int,
                            attempt: int, timeout: float) -> dict:
        base = {
            "step_id": step.step_id,
            "tool": step.tool,
            "order": index + 1,
            "attempt": attempt,
            "retries_used": 0,
        }
        try:
            result = await asyncio.wait_for(
                self.registry.execute(step.tool, dict(params)), timeout=timeout)
        except asyncio.TimeoutError:
            return {**base, "status": STEP_FAILED, "ok": False,
                    "error_code": "STEP_TIMEOUT", "error_type": "TimeoutError",
                    "summary": f"Timeout de {timeout:.0f}s no step '{step.step_id}'"}
        except Exception as exc:  # noqa: BLE001 - fail closed per step
            return {**base, "status": STEP_FAILED, "ok": False,
                    "error_code": "STEP_EXCEPTION", "error_type": type(exc).__name__,
                    "summary": redact_secrets(str(exc)[:200])}

        ok = bool(result.ok)
        data = result.data if hasattr(result, "data") else {}
        risk = getattr(result, "risk", "")
        approval_pending = isinstance(data, dict) and (
            data.get("error_code") == "APPROVAL_REQUIRED" or data.get("approval_id"))
        record = {
            **base,
            "ok": ok,
            "risk": risk.value if hasattr(risk, "value") else str(risk),
            "summary": _step_summary(data),
            "output": _cap_output(data),
        }
        if approval_pending:
            record["status"] = STEP_WAITING_USER
            record["approval_id"] = data.get("approval_id")
            return record
        if not ok:
            record["status"] = STEP_FAILED
            record["error_code"] = str(data.get("error_code") or "STEP_FAILED")[:60] \
                if isinstance(data, dict) else "STEP_FAILED"
            return record

        record["status"] = STEP_SUCCEEDED
        probe = step.verification_probe
        if probe is not None:
            verified = await self._probe(probe)
            record["verification_status"] = "VERIFIED" if verified else "VERIFICATION_FAILED"
            record["status"] = STEP_VERIFIED if verified else STEP_FAILED
            if not verified:
                record["error_code"] = "VERIFICATION_FAILED"
        elif step.verification:
            record["verification_status"] = "EXECUTED"  # declarativa apenas
        return record

    async def _probe(self, probe: VerificationProbe) -> bool:
        try:
            result = await asyncio.wait_for(
                self.registry.execute(probe.tool, dict(probe.params)), timeout=30)
        except Exception:  # noqa: BLE001
            return False
        if not result.ok:
            return False
        if not probe.expect_contains:
            return True
        haystack = json.dumps(result.data, ensure_ascii=False).casefold() \
            if hasattr(result, "data") else str(getattr(result, "data", "")).casefold()
        return probe.expect_contains.casefold() in haystack

    async def _run_rollback(self, step: WorkflowStep) -> bool:
        try:
            result = await asyncio.wait_for(
                self.registry.execute(step.rollback.tool, dict(step.rollback.params)),
                timeout=60)
            return bool(result.ok)
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------------- resume
    async def resume(self, run_id: str) -> dict:
        """Continue an interrupted run without repeating VERIFIED steps (§53-54)."""
        record = await self.history_store.get(run_id)
        if record is None:
            return {"success": False, "error_code": "RUN_NOT_FOUND"}
        if record["state"] in {RUN_STATE_SUCCEEDED, RUN_STATE_RUNNING}:
            return {"success": False, "error_code": "RUN_NOT_RESUMABLE",
                    "state": record["state"]}
        workflow = self._workflows.get(record["workflow_id"])
        if workflow is None:
            return {"success": False, "error_code": "WORKFLOW_NOT_FOUND"}
        lock = self._run_locks.setdefault(workflow.workflow_id, asyncio.Lock())
        async with lock:
            return await self._run_locked(workflow, record.get("parameters") or {},
                                          turn_id=None, agent_run_id=None,
                                          resumed_from=record)

    async def _approval_granted(self, approval_id: str) -> bool:
        lookup = getattr(self, "approval_lookup", None)
        if lookup is None:
            return False
        try:
            record = lookup(approval_id)
            status = getattr(record, "status", None)
            if status is None and isinstance(record, dict):
                status = record.get("status")
            return str(status or "").upper() == "GRANTED"
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------- history
    async def history(self, run_id: str | None = None, *,
                      limit: int = 25) -> dict:
        await self.history_store.initialize()
        if run_id:
            record = await self.history_store.get(run_id)
            if record is None:
                return {"success": False, "error_code": "RUN_NOT_FOUND"}
            return {"success": True, "runs": [record], "count": 1}
        items = await self.history_store.list(limit=limit)
        return {"success": True, "runs": items, "count": len(items)}

    async def cancel(self, run_id: str) -> dict:
        record = await self.history_store.get(run_id)
        if record is None:
            return {"success": False, "error_code": "RUN_NOT_FOUND"}
        if record["state"] not in {RUN_STATE_RUNNING, RUN_STATE_WAITING_USER}:
            return {"success": False, "error_code": "RUN_NOT_CANCELLABLE",
                    "state": record["state"]}
        record["state"] = RUN_STATE_CANCELLED
        record["finished_at"] = _utcnow_iso()
        await self.history_store.save(record)
        await self._emit("WORKFLOW_FINISHED", workflow_id=record["workflow_id"],
                         run_id=run_id, state="CANCELLED")
        return {"success": True, "run_id": run_id, "state": RUN_STATE_CANCELLED}

    # --------------------------------------------------------------------- events
    async def _emit(self, event_name: str, **payload: Any) -> None:
        if self.event_bus is None:
            return
        from app.events import EventType

        try:
            event = EventType(event_name)
        except ValueError:
            event = EventType.ERROR
        try:
            await asyncio.shield(self.event_bus.publish(event, **payload))
        except Exception:  # noqa: BLE001
            pass


def _cap_output(data: Any, limit: int = 4000) -> Any:
    """Keep bounded step outputs for binding/history without leaking secrets."""
    if data is None:
        return None
    try:
        text = json.dumps(redact_secrets(data), ensure_ascii=False)
    except (TypeError, ValueError):
        return redact_secrets(str(data))[:limit]
    if len(text) <= limit:
        return data if isinstance(data, (str, int, float, bool)) else redact_secrets(data)
    return {"truncated": True, "preview": text[:limit]}


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
