from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.intelligence.capabilities import CapabilityRegistryV2


class SkillManifest(BaseModel):
    identity: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,80}$")
    description: str = Field(min_length=3, max_length=500)
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    risk_class: str = Field(default="READ_ONLY", pattern=r"^(READ_ONLY|LOW_RISK|ELEVATED|DESTRUCTIVE|CRITICAL)$")
    dependencies: list[str] = Field(default_factory=list)
    health_checks: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    supported_actions: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)
    enabled: bool = True


class SkillCatalog:
    def __init__(self, root: Path, legacy_registry, tools, capabilities: CapabilityRegistryV2) -> None:
        self.root = root
        self.legacy_registry = legacy_registry
        self.tools = tools
        self.capabilities = capabilities
        self._manifests: dict[str, SkillManifest] = {}
        self._errors: list[dict[str, str]] = []

    def discover(self) -> dict[str, Any]:
        manifests: dict[str, SkillManifest] = {}
        errors: list[dict[str, str]] = []
        if self.root.is_dir():
            for path in sorted((*self.root.rglob("*.yaml"), *self.root.rglob("*.yml"), *self.root.rglob("*.json"))):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else yaml.safe_load(path.read_text(encoding="utf-8"))
                    values = raw.get("skills", []) if isinstance(raw, dict) else raw
                    if not isinstance(values, list):
                        raise ValueError("manifest root must be a list or skills object")
                    for value in values:
                        manifest = SkillManifest.model_validate(value)
                        if manifest.identity in manifests:
                            raise ValueError(f"duplicate skill: {manifest.identity}")
                        manifests[manifest.identity] = manifest
                except Exception as error:
                    errors.append({"path": path.relative_to(self.root).as_posix(), "error_code": type(error).__name__, "message": str(error)[:200]})
        self._manifests, self._errors = manifests, errors
        return {"discovered": len(manifests), "errors": errors}

    async def list(self) -> dict[str, Any]:
        if not self._manifests:
            self.discover()
        tool_names = {item["name"] for item in self.tools.descriptions()}
        legacy = {item["name"]: item for item in self.legacy_registry.list()}
        capability_snapshot = await self.capabilities.snapshot()
        capability_states = {item["id"]: item["state"] for item in capability_snapshot["capabilities"]}
        values = []
        for manifest in self._manifests.values():
            missing_tools = [name for name in manifest.tools if name not in tool_names]
            missing_dependencies = [name for name in manifest.dependencies if capability_states.get(name) not in {"AVAILABLE", "DEGRADED"}]
            executable = manifest.identity in legacy
            if not manifest.enabled:
                state = "DISABLED"
            elif missing_tools or missing_dependencies:
                state = "BLOCKED"
            elif executable:
                state = "AVAILABLE"
            else:
                state = "DEGRADED"
            values.append({**manifest.model_dump(mode="json"), "state": state, "executable": executable,
                           "missing_tools": missing_tools, "missing_dependencies": missing_dependencies})
        return {"skills": values, "errors": self._errors, "source": str(self.root.name)}

    async def execute(self, identity: str, payload: dict[str, Any], *, confirmed: bool = False):
        if identity not in self._manifests:
            self.discover()
        manifest = self._manifests.get(identity)
        if not manifest:
            raise KeyError("SKILL_NOT_FOUND")
        state = next((item for item in (await self.list())["skills"] if item["identity"] == identity), None)
        if not state or state["state"] != "AVAILABLE":
            raise RuntimeError("SKILL_NOT_AVAILABLE")
        return await self.legacy_registry.execute(identity, payload, confirmed=confirmed)
