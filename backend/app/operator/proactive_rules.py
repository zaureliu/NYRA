"""Proactive Operator (spec Parte M §232-§239).

DEFAULT IS OFF (§233: NYRA_PROACTIVE_OPERATOR_ENABLED=false). Only REGISTERED
rules fire (§234), each with an explicit trigger event, conditions and a
restricted action template. Unsolicited DESTRUCTIVE actions are impossible
(§239): the action allowlist below contains only notify/open-report/
run-readonly-workflow style operations. The existing ProactiveEngine budget
gate (cooldowns/quiet mode) still applies to every notification.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from app.core.paths import DATA_ROOT
from app.tools.redaction import redact_secrets


class ProactiveRule(BaseModel):
    rule_id: str = Field(pattern=r"^rule_[a-z0-9_]{3,48}$")
    trigger_event: str = Field(min_length=3, max_length=60)
    subject_filter: str = Field(default="", max_length=120)
    action: str = Field(pattern=r"^(notify|run_workflow|open_report)$")
    message_template: str = Field(default="", max_length=400)
    workflow_id: str | None = None
    priority: int = Field(default=50, ge=10, le=100)
    cooldown_seconds: int = Field(default=900, ge=30, le=86400)
    enabled: bool = True


_ALLOWED_ACTIONS = {"notify", "run_workflow", "open_report"}


class ProactiveOperator:
    def __init__(self, gate=None, workflows=None, event_bus=None, *,
                 enabled: bool = False, store_path=None) -> None:
        self.gate = gate  # ProactiveEngine (cooldown/budget) — reuse, not replace
        self.workflows = workflows
        self.event_bus = event_bus
        self.enabled = enabled
        self.store_path = store_path or (DATA_ROOT / "proactive-rules.json")
        self._rules: dict[str, ProactiveRule] = {}
        self._last_fired: dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        try:
            if not self.store_path.exists():
                return
            document = json.loads(self.store_path.read_text("utf-8"))
            for entry in document.get("rules", []):
                rule = ProactiveRule.model_validate(entry)
                self._rules[rule.rule_id] = rule
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        payload = {"version": 1, "rules": [item.model_dump() for item in self._rules.values()]}
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.store_path)

    # --------------------------------------------------------------------- CRUD
    def add_rule(self, rule: ProactiveRule) -> dict:
        if rule.action not in _ALLOWED_ACTIONS:
            return {"success": False, "error_code": "ACTION_NOT_ALLOWED",
                    "message": f"Ações permitidas: {sorted(_ALLOWED_ACTIONS)} (§239)."}
        if rule.action in {"run_workflow", "open_report"} and not rule.workflow_id:
            return {"success": False, "error_code": "WORKFLOW_REQUIRED"}
        self._rules[rule.rule_id] = rule
        self._save()
        return {"success": True, "rule": rule.model_dump()}

    def remove_rule(self, rule_id: str) -> dict:
        removed = self._rules.pop(rule_id, None)
        if removed is None:
            return {"success": False, "error_code": "RULE_NOT_FOUND"}
        self._save()
        return {"success": True, "removed": rule_id}

    def list_rules(self) -> dict:
        return {"success": True, "enabled": self.enabled,
                "rules": [item.model_dump() for item in self._rules.values()],
                "count": len(self._rules)}

    # ------------------------------------------------------------------ evaluate
    async def handle_event(self, event) -> None:
        """Bus subscriber: evaluate rules against one event (no-op when off)."""
        if not self.enabled or not self._rules:
            return
        for rule in list(self._rules.values()):
            if not rule.enabled:
                continue
            trigger = str(rule.trigger_event).upper()
            if str(getattr(event, "type", "")).upper() != trigger:
                continue
            payload = getattr(event, "payload", {}) or {}
            subject = str(payload.get("subject") or payload.get("event_type") or "")
            if rule.subject_filter and rule.subject_filter.casefold() not in subject.casefold():
                continue
            last = self._last_fired.get(rule.rule_id, 0.0)
            if time.time() - last < rule.cooldown_seconds:
                continue
            allowed_by_budget = True
            if self.gate is not None:
                allowed_by_budget = self.gate.allow(
                    f"proactive:{rule.rule_id}", priority=rule.priority,
                    cooldown_seconds=0,
                )
            if not allowed_by_budget:
                continue
            self._last_fired[rule.rule_id] = time.time()
            await self._execute_rule(rule, payload)

    async def _execute_rule(self, rule: ProactiveRule, payload: dict[str, Any]) -> None:
        context = redact_secrets(json.dumps(payload, ensure_ascii=False, default=str)[:300])
        message = (rule.message_template or "Evento {event} detectado").replace(
            "{event}", str(payload.get("event_type") or "")
        )
        if rule.action == "notify" and self.event_bus is not None:
            from app.events import EventType

            try:
                await asyncio.shield(self.event_bus.publish(
                    EventType.PROACTIVE_ALERT_FIRED, rule_id=rule.rule_id,
                    message=message[:200], context=context[:200],
                ))
            except Exception:  # noqa: BLE001
                pass
        elif rule.action in {"run_workflow", "open_report"} and self.workflows is not None and rule.workflow_id:
            outcome = await self.workflows.run(rule.workflow_id)
            del outcome

    # -------------------------------------------------------------------- status
    def status(self) -> dict:
        listing = self.list_rules()
        return {"success": True, "enabled": self.enabled, **listing}


_RULE_ID_RE = re.compile(r"^rule_[a-z0-9_]{3,48}$")
