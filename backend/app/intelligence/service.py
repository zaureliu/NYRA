from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.capabilities import get_capabilities
from app.core.paths import CONFIG_ROOT, DATA_ROOT, PROJECT_ROOT, RUNTIME_ROOT
from app.intelligence.capabilities import CapabilityRegistryV2
from app.intelligence.context import ContextEngine
from app.intelligence.diagnostics import DiagnosticsEngine
from app.intelligence.evaluation import EvaluationSuite
from app.intelligence.events import EventIntelligenceEngine
from app.intelligence.knowledge import KnowledgeEngine
from app.intelligence.memory import MemoryV2Service
from app.intelligence.models import MemoryKind, MemoryWrite, RuntimeState, Sensitivity
from app.intelligence.router import ModelRouterV2
from app.intelligence.skills import SkillCatalog
from app.intelligence.storage import IntelligenceStore
from app.intelligence.tasks import AutonomousTaskEngine
from app.intelligence.tracing import RuntimeTraceObserver, TraceService
from app.intelligence.vision_adapter import LocalVisionAdapter


class IntelligencePlatform:
    """Integrated local intelligence stack; existing policy/tool owners remain authoritative."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.store = IntelligenceStore(services.settings.database_path)
        self.memory = MemoryV2Service(self.store)
        self.knowledge = KnowledgeEngine(
            self.store,
            allowed_roots=(PROJECT_ROOT, DATA_ROOT),
        )
        self.router = ModelRouterV2(services.brain)
        self.capabilities = CapabilityRegistryV2(lambda: get_capabilities(services))
        self.context = ContextEngine(
            self.memory, self.knowledge,
            budget_characters=min(24_000, max(4_000, int(services.settings.ollama_context_size * 2.5))),
            capability_provider=self.capabilities.snapshot,
            runtime_provider=self.runtime_snapshot,
        )
        self.skills = SkillCatalog(CONFIG_ROOT / "skills", services.skills, services.tools, self.capabilities)
        self.events = EventIntelligenceEngine(self.store, services.event_bus)
        self.diagnostics = DiagnosticsEngine()
        self.traces = TraceService(self.store)
        self.runtime_traces = RuntimeTraceObserver(services.event_bus, self.traces)
        self.tasks = AutonomousTaskEngine(
            self.store, self.capabilities, self.traces, event_bus=services.event_bus,
        )
        self.vision = LocalVisionAdapter(
            self.router, base_url=services.settings.ollama_url,
            timeout_seconds=min(90, services.settings.llm_timeout_seconds),
            allowed_roots=(PROJECT_ROOT, DATA_ROOT),
        )
        self.evaluations = EvaluationSuite(RUNTIME_ROOT / "reports" / "evaluations")
        self.started_at = datetime.now(timezone.utc)
        self._started = False

    async def initialize(self) -> None:
        await self.store.initialize()
        self._register_capabilities()
        self._register_diagnostics()
        self._register_tasks()
        self._register_evaluations()
        self.skills.discover()
        await self.events.start()
        await self.runtime_traces.start()
        await self.tasks.start()
        if hasattr(self.services.brain, "model_router"):
            self.services.brain.model_router = self.router
        else:
            setattr(self.services.brain, "model_router", self.router)
        self._started = True

    async def stop(self) -> None:
        await self.tasks.stop()
        await self.runtime_traces.stop()
        await self.events.stop()
        self._started = False

    def _register_capabilities(self) -> None:
        self.capabilities.register("memory_v2", "Memória seletiva com decay, conflitos e provenance.", self._store_probe)
        self.capabilities.register("rag_local", "Indexação e recuperação local incremental.", self._rag_probe, dependencies=("memory_v2",))
        self.capabilities.register("context_engine", "Montagem de contexto priorizada e limitada.", self._ready_probe, dependencies=("memory_v2", "rag_local"))
        self.capabilities.register("model_router_v2", "Roteamento pelo inventário real do Ollama.", self._router_probe)
        self.capabilities.register("skill_catalog", "Catálogo dinâmico de Skills validado.", self._skills_probe)
        self.capabilities.register("autonomous_tasks_v2", "Tasks persistentes, limitadas e verificadas.", self._ready_probe)
        self.capabilities.register("event_intelligence", "Correlação de eventos sem afirmar causalidade.", self._ready_probe)
        self.capabilities.register("diagnostics_engine", "Diagnóstico baseado em checks observáveis.", self._ready_probe)
        self.capabilities.register("trace_replay", "Trace redacted e replay seguro.", self._ready_probe)
        self.capabilities.register("prompt_injection_defense_v2", "Fronteiras explícitas de confiança.", self._ready_probe)
        self.capabilities.register("action_budget", "Limites centralizados para ações autônomas.", self._ready_probe)
        self.capabilities.register("local_vision_model", "Modelo local opcional para screenshots.", self.vision.status, dependencies=("vision",))

    def _register_diagnostics(self) -> None:
        self.diagnostics.register("nyra", "intelligence_store", self._store_check)
        self.diagnostics.register("nyra", "capability_registry", self._capability_check)
        self.diagnostics.register("ollama", "brain_health", self._brain_check)
        self.diagnostics.register("memory", "database", self._store_check)
        self.diagnostics.register("rag", "index", self._rag_check)
        self.diagnostics.register("desktop", "desktop_controller", self._desktop_check)
        self.diagnostics.register("browser", "browser_operator", self._browser_check)
        self.diagnostics.register("voice", "conversation_engine", self._voice_check)
        self.diagnostics.register("network", "network_watch", self._network_check)
        self.diagnostics.register("selfdev", "selfdev_engine", self._selfdev_check)

    def _register_tasks(self) -> None:
        async def diagnostic(parameters: dict[str, Any]) -> dict[str, Any]:
            result = await self.diagnostics.run(str(parameters.get("domain") or "nyra"))
            return {"success": True, "effect_verified": True, "diagnosis": result.model_dump(mode="json")}

        async def rag_ingest(parameters: dict[str, Any]) -> dict[str, Any]:
            result = await self.knowledge.ingest_tree(Path(str(parameters.get("root") or PROJECT_ROOT)),
                                                      max_files=min(500, max(1, int(parameters.get("max_files", 100)))))
            return {"success": True, "effect_verified": True, **result}

        async def capability_snapshot(_: dict[str, Any]) -> dict[str, Any]:
            return {"success": True, "effect_verified": True, **(await self.capabilities.snapshot())}

        self.tasks.register("diagnostic", diagnostic, risk="READ_ONLY")
        self.tasks.register("rag_incremental_ingest", rag_ingest, risk="LOW_RISK")
        self.tasks.register("capability_snapshot", capability_snapshot, risk="READ_ONLY")

    def _register_evaluations(self) -> None:
        async def storage() -> dict[str, Any]:
            value = await self.store.health()
            return {"success": bool(value.get("ok")), "effect_verified": bool(value.get("ok")), **value}

        async def memory_cycle() -> dict[str, Any]:
            created = await self.memory.write(MemoryWrite(
                kind=MemoryKind.EPISODIC, content="NYRA evaluation marker for selective memory",
                source="evaluation", category="evaluation", confidence=.95, relevance=.95,
                sensitivity=Sensitivity.INTERNAL,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ), force=True)
            memory_id = created.get("memory", {}).get("id")
            found = await self.memory.retrieve("evaluation marker selective memory", limit=5)
            if memory_id:
                await self.memory.delete(memory_id)
            ok = any(item.id == memory_id for item in found)
            return {"success": ok, "effect_verified": ok, "created": bool(memory_id), "retrieved": ok}

        async def model_route() -> dict[str, Any]:
            route = await self.router.route("Explique a arquitetura deste código")
            ok = route.selected_model is not None
            return {"success": ok, "effect_verified": ok, "route": route.model_dump(mode="json")}

        async def capabilities() -> dict[str, Any]:
            value = await self.capabilities.snapshot()
            ok = bool(value.get("capabilities"))
            return {"success": ok, "effect_verified": ok, "summary": value.get("summary")}

        self.evaluations.register("persistence.memory", "REAL", storage)
        self.evaluations.register("memory.write_retrieve_delete", "REAL", memory_cycle)
        self.evaluations.register("model.live_routing", "REAL", model_route)
        self.evaluations.register("capabilities.live_discovery", "REAL", capabilities)
        self.evaluations.register("security.prompt_injection_boundary", "SIMULATED", self.evaluations.injection_boundary_scenario)

    async def runtime_snapshot(self) -> dict[str, Any]:
        operator = self.services.operator_v2.status() if self.services.operator_v2 else {"operator_v2": False}
        return {
            "state": "AVAILABLE" if self._started else "OFFLINE",
            "agent": self.services.agent.status(),
            "operator": operator,
            "selfdev": self.services.selfdev.status(),
        }

    async def status(self) -> dict[str, Any]:
        store, counts, capabilities, vision = await asyncio.gather(
            self.store.health(), self.store.counts(), self.capabilities.snapshot(), self.vision.status()
        )
        route = self.router.last_route.model_dump(mode="json") if self.router.last_route else None
        tasks = await self.tasks.list(include_terminal=False)
        return {
            "state": "ONLINE" if self._started and store.get("ok") else "DEGRADED",
            "started_at": self.started_at.isoformat(), "storage": store, "counts": counts,
            "capabilities": capabilities, "model_router": {"last_route": route},
            "context": {"budget_characters": self.context.budget_characters,
                        "recent_assemblies": self.context.diagnostics()[-5:]},
            "rag": self.knowledge.status(), "vision": vision,
            "tasks": {"active_or_queued": len(tasks), "last_error": self.tasks.last_error},
            "diagnostic_domains": self.diagnostics.domains(),
            "trace": {"dropped_events": self.runtime_traces.dropped,
                      "persist_failures": self.runtime_traces.persist_failures,
                      "last_error": self.runtime_traces.last_error},
            "events": {"dropped": self.events.dropped, "coalesced": self.events.coalesced,
                       "persist_failures": self.events.persist_failures,
                       "last_error": self.events.last_error},
            "evaluation": self.evaluations.last_report.get("summary") if self.evaluations.last_report else None,
        }

    async def _store_probe(self) -> dict[str, Any]:
        value = await self.store.health()
        return {"state": "AVAILABLE" if value.get("ok") else "OFFLINE", "health": "READY" if value.get("ok") else "FAILED", "details": value}

    async def _router_probe(self) -> dict[str, Any]:
        inventory = await self.router.inventory()
        ok = bool(inventory.get("ollama_ready") and inventory.get("models"))
        return {"state": "AVAILABLE" if ok else "OFFLINE", "health": "READY" if ok else "OLLAMA_UNAVAILABLE",
                "details": {"models": len(inventory.get("models", []))}}

    async def _skills_probe(self) -> dict[str, Any]:
        # Do not call SkillCatalog.list() while CapabilityRegistry owns its
        # snapshot lock: list() itself resolves capability dependencies.
        # Discovery is filesystem-only and therefore breaks the dependency
        # cycle while still reporting manifest health truthfully.
        value = self.skills.discover()
        errors = value.get("errors", [])
        discovered = int(value.get("discovered", 0))
        return {"state": "AVAILABLE" if discovered and not errors else "DEGRADED",
                "health": "READY" if not errors else "MANIFEST_ERRORS",
                "details": {"skills": discovered, "errors": len(errors)}}

    async def _rag_probe(self) -> dict[str, Any]:
        return {"state": "AVAILABLE", "health": "READY", "details": self.knowledge.status()}

    @staticmethod
    def _ready_probe() -> dict[str, Any]:
        return {"state": RuntimeState.AVAILABLE.value, "health": "READY"}

    async def _store_check(self) -> dict[str, Any]:
        value = await self.store.health()
        return {"ok": bool(value.get("ok")), **value}

    async def _capability_check(self) -> dict[str, Any]:
        value = await self.capabilities.snapshot()
        return {"ok": bool(value.get("capabilities")), "summary": value.get("summary")}

    async def _brain_check(self) -> dict[str, Any]:
        return {"ok": await self.services.brain.health(), "provider": self.services.brain.name}

    def _rag_check(self) -> dict[str, Any]:
        return {"ok": True, **self.knowledge.status()}

    def _desktop_check(self) -> dict[str, Any]:
        return {"ok": self.services.desktop is not None, "initialized": self.services.desktop is not None}

    def _browser_check(self) -> dict[str, Any]:
        enabled = bool(self.services.operator_v2 and self.services.operator_v2.browser_control_enabled)
        return {"ok": enabled, "state": "AVAILABLE" if enabled else "DISABLED"}

    def _voice_check(self) -> dict[str, Any]:
        return {"ok": self.services.conversation is not None, "state": str(self.services.conversation.state.value)}

    def _network_check(self) -> dict[str, Any]:
        value = self.services.network_watch.status()
        return {"ok": bool(value.get("enabled")), **value}

    def _selfdev_check(self) -> dict[str, Any]:
        value = self.services.selfdev.status()
        return {"ok": value.get("state") not in {"FAILED", "BLOCKED"}, **value}
