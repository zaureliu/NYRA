"""Action policy for homelab mutations (spec §82-§88).

Risk levels and approval modes are policy-driven per resource, defaulting to
fail-closed: anything unknown requires approval. VM destruction is NOT exposed
in V1 (spec §83).
"""

from __future__ import annotations

from app.homelab.models import ActionDecision


# action -> (risk_level, approval_mode) where mode: auto | approval
_DEFAULT_POLICIES: dict[str, tuple[str, str]] = {
    "vm_start": ("LOW_RISK", "approval"),
    "vm_shutdown": ("ELEVATED", "approval"),
    "vm_stop": ("DESTRUCTIVE", "approval"),
    "vm_reboot": ("ELEVATED", "approval"),
    "vm_reset": ("DESTRUCTIVE", "approval"),
    "ha_call_service": ("LOW_RISK", "approval"),
}

_ALLOWED_ACTIONS = frozenset(_DEFAULT_POLICIES)


def normalize_action(action: str) -> str | None:
    value = str(action or "").strip().casefold()
    return value if value in _ALLOWED_ACTIONS else None


def decide(action: str, host_policy: dict | None = None) -> ActionDecision:
    """Resolve risk + approval requirement for one action.

    Host-level overrides (registry YAML `risk_policy`) may only make actions
    MORE restrictive than the built-in defaults; they can never downgrade
    approval requirements.
    """
    normalized = normalize_action(action)
    if normalized is None:
        return ActionDecision(
            action=str(action)[:60],
            risk_level="ELEVATED",
            requires_approval=True,
            reason="Ação desconhecida é fail-closed e exige aprovação explícita.",
        )
    default_risk, mode = _DEFAULT_POLICIES[normalized]
    override = {}
    if isinstance(host_policy, dict):
        raw = host_policy.get(normalized)
        if isinstance(raw, str):
            override["mode"] = raw.strip().casefold()
        elif isinstance(raw, dict):
            mode_value = raw.get("mode") or raw.get("approval")
            if isinstance(mode_value, str):
                override["mode"] = mode_value.strip().casefold()
            risk_value = raw.get("risk")
            if isinstance(risk_value, str) and _RISK_RANK.get(risk_value.strip().upper()) is not None:
                override["risk"] = risk_value.strip().upper()
    effective_mode = override.get("mode", mode)
    # Overrides may only tighten: auto -> approval allowed, never approval -> auto.
    if mode == "approval":
        effective_mode = "approval"
    elif effective_mode not in {"auto", "approval"}:
        effective_mode = "approval"
    effective_risk = default_risk
    requested_risk = override.get("risk")
    if requested_risk:
        current_rank = _RISK_RANK[default_risk]
        requested_rank = _RISK_RANK.get(requested_risk, current_rank)
        if requested_rank > current_rank:
            effective_risk = requested_risk
    reason = (
        "Autorizada por política do homelab."
        if effective_mode == "auto"
        else "Requer confirmação explícita do operador."
    )
    return ActionDecision(
        action=normalized,
        risk_level=effective_risk,
        requires_approval=effective_mode == "approval",
        reason=reason,
    )


_RISK_RANK = {
    "READ_ONLY": 0,
    "LOW_RISK": 1,
    "ELEVATED": 2,
    "DESTRUCTIVE": 3,
    "CRITICAL": 4,
}
