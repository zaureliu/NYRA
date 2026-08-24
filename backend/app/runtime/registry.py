"""Service registry: trusted definitions loaded from config, validated per spec #76."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.runtime.models import RuntimeType, ServiceSpec

_TOKEN_SUBSTITUTIONS = ("{python}", "{repo_root}")


@dataclass
class RegistryEntry:
    service_id: str
    spec: ServiceSpec | None = None
    error: str | None = None


@dataclass
class RuntimeRegistry:
    entries: list[RegistryEntry] = field(default_factory=list)
    source_path: str = ""

    def get(self, service_id: str) -> ServiceSpec | None:
        for entry in self.entries:
            if entry.service_id == service_id and entry.spec is not None:
                return entry.spec
        return None

    def error_for(self, service_id: str) -> str | None:
        for entry in self.entries:
            if entry.service_id == service_id:
                return entry.error
        return None

    def valid_specs(self) -> list[ServiceSpec]:
        return [entry.spec for entry in self.entries if entry.spec is not None]

    def ids(self) -> list[str]:
        return [entry.service_id for entry in self.entries]


def _substitute(value: Any, python_exe: str, repo_root: str) -> Any:
    if isinstance(value, str):
        return value.replace("{python}", python_exe).replace("{repo_root}", repo_root)
    if isinstance(value, list):
        return [_substitute(item, python_exe, repo_root) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, python_exe, repo_root) for key, item in value.items()}
    return value


def load_runtime_registry(path: Path, *, python_exe: str, repo_root: str) -> RuntimeRegistry:
    registry = RuntimeRegistry(source_path=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    except (yaml.YAMLError, OSError):
        registry.entries.append(RegistryEntry(service_id="registry_file", error="REGISTRY_FILE_UNREADABLE"))
        return registry
    services = raw.get("services")
    if not isinstance(services, list):
        services = []
    seen: dict[str, int] = {}
    specs: dict[str, ServiceSpec] = {}

    for position, item in enumerate(services):
        if not isinstance(item, dict) or not str(item.get("id", "")).strip():
            registry.entries.append(RegistryEntry(service_id=f"<invalid:{position}>", error="MISSING_SERVICE_ID"))
            continue
        service_id = str(item["id"])
        substituted = _substitute(item, python_exe, str(repo_root))
        try:
            spec = ServiceSpec.model_validate(substituted)
        except Exception as exc:
            registry.entries.append(RegistryEntry(
                service_id=service_id,
                error=f"INVALID_CONFIGURATION: {type(exc).__name__}: {str(exc)[:200]}",
            ))
            continue
        validation_error = _validate_spec(spec)
        if validation_error is None and service_id in seen:
            validation_error = f"DUPLICATE_SERVICE_ID (first defined at position {seen[service_id]})"
        seen.setdefault(service_id, position)
        registry.entries.append(RegistryEntry(service_id=service_id, spec=None if validation_error else spec, error=validation_error))
        if validation_error is None:
            specs[service_id] = spec

    _validate_dependencies(registry, specs)
    return registry


def _validate_spec(spec: ServiceSpec) -> str | None:
    if spec.type == RuntimeType.PROCESS:
        if spec.capabilities.start:
            if not spec.start_command or len(spec.start_command) < 1:
                return "INVALID_CONFIGURATION: start_command ausente para PROCESS com capability start"
            if not spec.working_directory:
                return "INVALID_CONFIGURATION: working_directory ausente para PROCESS com capability start"
    health = spec.health
    if health is None or health.kind.value == "NONE":
        if not spec.capabilities.health:
            return None
        return "INVALID_CONFIGURATION: capability health exige bloco health"
    if health.kind in {"TCP"} and not health.port:
        return f"INVALID_CONFIGURATION: health {health.kind.value} exige port"
    if health.kind == "PROCESS" and not (health.port or health.process_match):
        return f"INVALID_CONFIGURATION: health {health.kind.value} exige port ou process_match"
    if health.kind == "HTTP" and not health.url:
        return "INVALID_CONFIGURATION: health HTTP exige url"
    if health.kind == "COMMAND" and not health.command:
        return "INVALID_CONFIGURATION: health COMMAND exige command"
    if health.kind in {"WARM_MANAGER", "SENTINEL"} and spec.type != RuntimeType.EXTERNAL_SERVICE:
        return f"INVALID_CONFIGURATION: health {health.kind.value} só vale para EXTERNAL_SERVICE"
    return None


def _validate_dependencies(registry: RuntimeRegistry, specs: dict[str, ServiceSpec]) -> None:
    for spec in specs.values():
        for dep in spec.depends_on:
            if dep not in specs:
                registry.entries.append(RegistryEntry(
                    service_id=spec.id,
                    error=f"INVALID_CONFIGURATION: dependência inexistente '{dep}'",
                ))

    state: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> str | None:
        status = state.get(node, 0)
        if status == 2:
            return None
        if status == 1:
            cycle = stack[stack.index(node):] + [node]
            return "INVALID_CONFIGURATION: ciclo de dependências: " + " -> ".join(cycle)
        state[node] = 1
        for dep in specs[node].depends_on:
            if dep in specs:
                failure = visit(dep, stack + [node])
                if failure:
                    return failure
        state[node] = 2
        return None

    for service_id in list(specs):
        if state.get(service_id, 0) == 0:
            failure = visit(service_id, [])
            if failure:
                target = specs.get(service_id)
                registry.entries.append(RegistryEntry(service_id=service_id, spec=None, error=failure))
