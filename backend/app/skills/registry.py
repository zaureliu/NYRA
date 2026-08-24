from __future__ import annotations

import time
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.events import EventBus, EventType
from app.realtime.cooldowns import CooldownManager
from app.skills.models import SkillDefinition, SkillPermission, SkillResult
from app.core.paths import DATA_ROOT


SkillHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SkillRegistry:
    def __init__(self, event_bus: EventBus, cooldowns: CooldownManager, state_path: Path | None = None) -> None:
        self.event_bus, self.cooldowns = event_bus, cooldowns
        self.state_path = state_path
        self._saved = self._load()
        self._definitions: dict[str, SkillDefinition] = {}
        self._handlers: dict[str, SkillHandler] = {}
        self._last_used: dict[str, float] = {}

    def register(self, definition: SkillDefinition, handler: SkillHandler) -> None:
        if definition.name in self._saved:
            saved = self._saved[definition.name]
            definition = definition.model_copy(update={
                "enabled": bool(saved.get("enabled", definition.enabled)),
                "cooldown_seconds": float(saved.get("cooldown_seconds", definition.cooldown_seconds)),
            })
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {**definition.model_dump(mode="json"), "last_used": self._last_used.get(name),
             "cooldown_remaining": self.cooldowns.remaining(f"skill:{name}", definition.cooldown_seconds)}
            for name, definition in self._definitions.items()
        ]

    def update(self, name: str, *, enabled: bool | None = None, cooldown_seconds: float | None = None) -> dict[str, Any]:
        definition = self._definitions.get(name)
        if not definition:
            raise KeyError(f"Skill desconhecida: {name}")
        values = {}
        if enabled is not None:
            values["enabled"] = enabled
        if cooldown_seconds is not None:
            values["cooldown_seconds"] = max(0, min(86400, cooldown_seconds))
        self._definitions[name] = definition.model_copy(update=values)
        self._save()
        return self._definitions[name].model_dump(mode="json")

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.state_path:
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        value = {name: {"enabled": item.enabled, "cooldown_seconds": item.cooldown_seconds} for name, item in self._definitions.items()}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    async def execute(self, name: str, payload: dict[str, Any] | None = None, *, confirmed: bool = False) -> SkillResult:
        definition = self._definitions.get(name)
        if not definition or not definition.available:
            raise KeyError(f"Skill indisponível: {name}")
        if not definition.enabled:
            raise PermissionError("Skill desabilitada")
        if definition.permission == SkillPermission.DANGEROUS:
            raise PermissionError("Skills perigosas permanecem bloqueadas")
        if definition.permission == SkillPermission.CONFIRM_REQUIRED and not confirmed:
            raise PermissionError("Confirmação explícita obrigatória")
        key = f"skill:{name}"
        if not self.cooldowns.ready(key, definition.cooldown_seconds, consume=True):
            raise RuntimeError("Skill em cooldown")
        started = time.perf_counter()
        data = await self._handlers[name](payload or {})
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        self._last_used[name] = time.time()
        await self.event_bus.publish(EventType.SKILL_TRIGGERED, skill=name, permission=definition.permission.value, ok=True, elapsed_ms=elapsed_ms)
        return SkillResult(name=name, ok=True, permission=definition.permission, data=data, elapsed_ms=elapsed_ms)


def create_skill_registry(*, event_bus: EventBus, cooldowns: CooldownManager, tools, perception,
                          network_watch, sentinel, memory, listening) -> SkillRegistry:
    registry = SkillRegistry(event_bus, cooldowns, DATA_ROOT / "skills-v4.json")

    async def tool_handler(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await tools.execute(name, payload)
        return result.model_dump(mode="json")

    tool_names = {
        "network_status": "get_network_status",
        "ping_host": "ping_host",
        "dns_lookup": "dns_lookup",
        "service_check": "check_http_service",
        "system_stats": "get_local_system_stats",
        "recent_alerts": "get_recent_network_events",
    }
    for skill_name, tool_name in tool_names.items():
        registry.register(
            SkillDefinition(name=skill_name, description=f"Executa a ferramenta local validada {tool_name}.", triggers=[skill_name], cooldown_seconds=1),
            lambda payload, selected=tool_name: tool_handler(selected, payload),
        )

    async def active_app(_: dict) -> dict:
        return perception.public_snapshot()["foreground_app"]
    async def idle(_: dict) -> dict:
        snap = perception.snapshot
        return {"user_activity": snap.user_activity, "idle_seconds": snap.idle_seconds}
    async def load(_: dict) -> dict:
        return perception.public_snapshot()["system"]
    async def sentinel_status(_: dict) -> dict:
        return sentinel.status()
    async def memory_search(payload: dict) -> dict:
        query = str(payload.get("query", "")).strip()[:300]
        if not query:
            raise ValueError("query obrigatória")
        values = await memory.search(query, limit=min(8, max(1, int(payload.get("limit", 5)))))
        return {"results": [item.model_dump(mode="json") for item in values]}
    async def ui(command: str, _: dict) -> dict:
        await event_bus.publish(EventType.UI_COMMAND, command=command)
        return {"command": command, "dispatched": True}
    async def mute(value: bool, _: dict) -> dict:
        return await listening.set_muted(value)

    registry.register(SkillDefinition(name="get_active_app", description="Lê somente a classificação do app em primeiro plano."), active_app)
    registry.register(SkillDefinition(name="get_idle_time", description="Lê o tempo de inatividade local atual."), idle)
    registry.register(SkillDefinition(name="get_system_load", description="Lê CPU, RAM e disco atuais."), load)
    registry.register(SkillDefinition(name="sentinel_status", description="Lê o estado atual do Utamo Sentinel."), sentinel_status)
    registry.register(SkillDefinition(name="memory_search", description="Pesquisa memória explícita da NYRA.", cooldown_seconds=.5), memory_search)
    for name, command in (("open_nyra_dashboard", "open_dashboard"), ("show_nyra", "show"), ("hide_nyra", "hide")):
        registry.register(SkillDefinition(name=name, description=f"Solicita à interface local: {command}."), lambda payload, selected=command: ui(selected, payload))
    registry.register(SkillDefinition(name="mute_nyra", description="Silencia a escuta local da NYRA."), lambda payload: mute(True, payload))
    registry.register(SkillDefinition(name="unmute_nyra", description="Reativa a escuta local da NYRA."), lambda payload: mute(False, payload))
    registry.register(
        SkillDefinition(name="open_application", description="Abre somente aplicativo cadastrado em allowlist explícita.", permission=SkillPermission.CONFIRM_REQUIRED, enabled=False, available=False, metadata={"allowlist": []}),
        lambda _: _unavailable(),
    )
    return registry


async def _unavailable() -> dict[str, Any]:
    raise RuntimeError("Skill preparada, mas sem allowlist configurada")
