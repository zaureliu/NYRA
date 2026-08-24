from __future__ import annotations

import re

from app.network_aliases import NetworkHostAlias
from app.tools.remote_models import RemotePolicyAssessment
from app.tools.shell_models import ShellRiskLevel
from app.tools.shell_risk import ShellRiskClassifier


class RemoteCommandPolicy:
    """Adds host capabilities and normalized remediation actions to shell risk."""

    def __init__(self, classifier: ShellRiskClassifier | None = None) -> None:
        self.classifier = classifier or ShellRiskClassifier()

    def assess(
        self,
        host: NetworkHostAlias,
        command: str,
        *,
        auto_remediation_enabled: bool,
        global_actions: set[str],
    ) -> RemotePolicyAssessment:
        base = self.classifier.classify(command, "bash")
        level = base.level
        reasons = list(base.reasons)
        if level == ShellRiskLevel.LOW_RISK:
            level = ShellRiskLevel.ELEVATED
            reasons.append("remote state changes have elevated impact")
        capability = self._capability(command)
        action, resource_type, resource_name = self._normalized_action(command)
        remote = host.remote_shell
        managed = set(remote.managed_resources.get(resource_type or "", []))
        resource_allowed = resource_name is None or resource_name in managed
        auto_allowed = bool(
            auto_remediation_enabled
            and action
            and action in global_actions
            and action in remote.auto_remediation_actions
            and resource_allowed
            and level in {ShellRiskLevel.LOW_RISK, ShellRiskLevel.ELEVATED}
        )
        return RemotePolicyAssessment(
            risk_level=level,
            reasons=list(dict.fromkeys(reasons)),
            required_capability=capability,
            normalized_action=action,
            resource_type=resource_type,
            resource_name=resource_name,
            auto_remediation_allowed=auto_allowed,
        )

    @staticmethod
    def _capability(command: str) -> str:
        value = command.casefold()
        if re.search(r"\b(?:journalctl|logread|dmesg)\b|(?:^|\s)(?:tail|head)\s+.*(?:log|journal)", value):
            return "logs"
        if re.search(r"\b(?:qm|pct|pveversion|pvesh)\b", value):
            return "virtualization"
        if re.search(r"\b(?:pvesm|zfs|zpool|lsblk|findmnt)\b", value):
            return "storage"
        if re.search(r"\b(?:docker|podman)\b", value):
            return "containers"
        if re.search(r"\b(?:systemctl|service)\b|/etc/init\.d/", value):
            return "service_management"
        if re.search(r"\b(?:ip|ubus|uci|wifi|iw|iwinfo|ifstatus)\b", value):
            return "network"
        return "diagnostics"

    @staticmethod
    def _normalized_action(command: str) -> tuple[str | None, str | None, str | None]:
        value = command.strip()
        match = re.search(r"(?i)\bsystemctl\s+(?:try-)?restart\s+([A-Za-z0-9_.@-]+)", value)
        if match:
            return "restart_known_service", "services", match.group(1)
        match = re.search(r"(?i)\bservice\s+([A-Za-z0-9_.@-]+)\s+restart\b", value)
        if match:
            return "restart_known_service", "services", match.group(1)
        match = re.search(r"(?i)/etc/init\.d/([A-Za-z0-9_.@-]+)\s+restart\b", value)
        if match:
            return "restart_known_service", "services", match.group(1)
        match = re.search(r"(?i)\b(?:docker|podman)(?:\s+compose)?\s+restart\s+([A-Za-z0-9_.@/-]+)", value)
        if match:
            return "restart_known_container", "containers", match.group(1)
        match = re.search(r"(?i)\bqm\s+start\s+(\d+)\b", value)
        if match:
            return "start_known_vm", "vms", match.group(1)
        match = re.search(r"(?i)\bpct\s+start\s+(\d+)\b", value)
        if match:
            return "start_known_container", "containers", match.group(1)
        if re.search(r"(?i)(?:^|[;&|]\s*)wifi\s+reload\b", value):
            return "reload_known_wifi", "network", "wifi"
        return None, None, None
