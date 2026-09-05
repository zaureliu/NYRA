"""KAZUMI Autonomous Computer Operator V2 (spec: prompt9_autonomous_computer_operator_v2.md).

Capabilities layered ON TOP of the existing Agent Controller — never a second
brain. Each sub-module is independently testable and follows the house rules:
Pydantic schemas, risk levels, single-use approvals, grounding, verification,
turn isolation, local-first, no secret exposure to the LLM.
"""

from __future__ import annotations

from app.operator.contexts import (
    ContextKind,
    CrossContextRejectionError,
    JobContext,
    OperatorContextRegistry,
    TaskContext,
    WatchContext,
    WorkflowContext,
)
from app.operator.service import OperatorV2Service, create_operator_v2_service

__all__ = [
    "ContextKind",
    "CrossContextRejectionError",
    "JobContext",
    "OperatorContextRegistry",
    "OperatorV2Service",
    "TaskContext",
    "WatchContext",
    "WorkflowContext",
    "create_operator_v2_service",
]
