from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable

from app.intelligence.knowledge import KnowledgeEngine
from app.intelligence.memory import MemoryV2Service
from app.intelligence.models import ContextAssembly, ContextBlock, MemoryKind, TrustBoundary
from app.intelligence.trust import envelope


SnapshotProvider = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]
RelevantStateProvider = Callable[[str], str | Awaitable[str]]


class ContextEngine:
    """Budgeted context assembly. System policy remains outside this budget."""

    def __init__(self, memory: MemoryV2Service, knowledge: KnowledgeEngine, *,
                 budget_characters: int = 12_000, capability_provider: SnapshotProvider | None = None,
                 runtime_provider: SnapshotProvider | None = None,
                 world_state_provider: RelevantStateProvider | None = None,
                 open_loop_provider: RelevantStateProvider | None = None) -> None:
        self.memory = memory
        self.knowledge = knowledge
        self.budget_characters = max(2_000, budget_characters)
        self.capability_provider = capability_provider
        self.runtime_provider = runtime_provider
        self.world_state_provider = world_state_provider
        self.open_loop_provider = open_loop_provider
        self._decisions: deque[dict[str, Any]] = deque(maxlen=100)

    async def assemble(self, request: str, *, project: str | None = None,
                       recent_conversation: list[dict[str, str]] | None = None,
                       include_runtime: bool = True) -> ContextAssembly:
        memory_task = self.memory.retrieve(request, project=project, limit=8)
        rag_task = self.knowledge.retrieve(request, limit=8)
        memories, knowledge = await asyncio.gather(memory_task, rag_task)
        candidates: list[ContextBlock] = []
        if recent_conversation:
            for index, message in enumerate(recent_conversation[-8:]):
                content = str(message.get("content") or "")[:2_000]
                if content:
                    candidates.append(ContextBlock(
                        source=f"conversation:{index}", content=content,
                        trust=TrustBoundary.USER_INPUT if message.get("role") == "user" else TrustBoundary.MEMORY_CONTENT,
                        priority=90, relevance=1, characters=len(content),
                    ))
        for item in memories:
            content = envelope(item.content, TrustBoundary.MEMORY_CONTENT, {"memory_id": item.id, "kind": item.kind.value})
            candidates.append(ContextBlock(
                source=f"memory:{item.id}", content=content, trust=TrustBoundary.MEMORY_CONTENT,
                priority=78 if item.kind in {MemoryKind.PROJECT, MemoryKind.USER_PREFERENCE} else 68,
                relevance=max(0, min(1, item.score)), characters=len(content),
                provenance={"memory_id": item.id, "kind": item.kind.value, "conflict": item.conflict},
            ))
        for hit in knowledge:
            content = envelope(hit.content, TrustBoundary.DOCUMENT_CONTENT, hit.provenance)
            candidates.append(ContextBlock(
                source=f"rag:{hit.chunk_id}", content=content, trust=TrustBoundary.DOCUMENT_CONTENT,
                priority=65, relevance=hit.score, characters=len(content), provenance=hit.provenance,
            ))
        world_state = await self._relevant_world_state(request)
        if world_state:
            content = envelope(
                world_state,
                TrustBoundary.TOOL_TRUSTED,
                {"source": "world_state", "grounded": True},
            )
            candidates.append(ContextBlock(
                source="world_state", content=content,
                trust=TrustBoundary.TOOL_TRUSTED,
                priority=88, relevance=.98, characters=len(content),
                provenance={"source": "world_state", "grounded": True},
            ))
        open_loop_context = await self._relevant_open_loop(request)
        if open_loop_context:
            content = envelope(
                open_loop_context,
                TrustBoundary.TOOL_TRUSTED,
                {"source": "open_loops_engine", "grounded": True,
                 "authorization": False},
            )
            candidates.append(ContextBlock(
                source="open_loops", content=content,
                trust=TrustBoundary.TOOL_TRUSTED,
                priority=89, relevance=.99, characters=len(content),
                provenance={"source": "open_loops_engine", "grounded": True,
                            "authorization": False},
            ))
        if include_runtime:
            capability, runtime = await asyncio.gather(
                self._snapshot(self.capability_provider), self._snapshot(self.runtime_provider)
            )
            if capability:
                content = envelope(self._compact(capability), TrustBoundary.TOOL_TRUSTED, {"source": "capability_registry"})
                candidates.append(ContextBlock(source="capabilities", content=content, trust=TrustBoundary.TOOL_TRUSTED,
                                               priority=82, relevance=0.8, characters=len(content)))
            if runtime:
                content = envelope(self._compact(runtime), TrustBoundary.TOOL_TRUSTED, {"source": "runtime_snapshot"})
                candidates.append(ContextBlock(source="runtime", content=content, trust=TrustBoundary.TOOL_TRUSTED,
                                               priority=72, relevance=0.7, characters=len(content)))

        ranked = sorted(candidates, key=lambda item: (item.priority, item.relevance), reverse=True)
        selected: list[ContextBlock] = []
        used = 0
        decisions: list[dict[str, Any]] = []
        for item in ranked:
            if used + item.characters > self.budget_characters:
                decisions.append({"source": item.source, "decision": "DROP_BUDGET", "characters": item.characters})
                continue
            selected.append(item)
            used += item.characters
            decisions.append({"source": item.source, "decision": "SELECT", "priority": item.priority, "relevance": item.relevance})
        assembly = ContextAssembly(
            blocks=selected, used_characters=used, budget_characters=self.budget_characters,
            dropped_blocks=len(ranked) - len(selected), decisions=decisions,
        )
        # Diagnostics describe selection policy, never selected content.
        # Memory/RAG blocks can contain private local data and must not leak
        # through the status endpoint consumed by Operations UI.
        self._decisions.append({
            "used_characters": assembly.used_characters,
            "budget_characters": assembly.budget_characters,
            "selected_blocks": len(assembly.blocks),
            "dropped_blocks": assembly.dropped_blocks,
            "decisions": assembly.decisions,
        })
        return assembly

    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._decisions)

    @staticmethod
    async def _snapshot(provider: SnapshotProvider | None) -> dict[str, Any]:
        if provider is None:
            return {}
        try:
            value = provider()
            if asyncio.iscoroutine(value):
                value = await value
            return value if isinstance(value, dict) else {}
        except Exception as error:
            return {"state": "DEGRADED", "error_code": type(error).__name__}

    async def _relevant_world_state(self, request: str) -> str:
        if self.world_state_provider is None:
            return ""
        try:
            value = self.world_state_provider(request)
            if asyncio.iscoroutine(value):
                value = await value
            return str(value or "")[:1800]
        except Exception:
            return ""

    async def _relevant_open_loop(self, request: str) -> str:
        if self.open_loop_provider is None:
            return ""
        try:
            value = self.open_loop_provider(request)
            if asyncio.iscoroutine(value):
                value = await value
            return str(value or "")[:2400]
        except Exception:
            return ""

    @staticmethod
    def _compact(value: dict[str, Any]) -> str:
        import json
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)[:8_000]
