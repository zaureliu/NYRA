r"""Camada 6 — UsageLearningService (kazumi-7c §50-§63).

Aprende PADRÕES OPERACIONAIS confirmados — nunca conteúdo privado (§52):
aliases, preferências (pasta/projeto por tarefa), sequências recorrentes
(workflow candidates) com thresholds (§59), sinais negativos de correção
(§60) e explicabilidade resumida (§61, sem chain-of-thought).

Só aprende com `verified_result=True` (§54). Storage FORA do repo em
%LOCALAPPDATA%\KAZUMI\usage-learning (§62) com retenção/compação (§63).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger("kazumi.computer.usage")

ALIAS_CONFIRM_THRESHOLD = 3       # §59: 3+ sucessos consistentes → candidato
WORKFLOW_HIGH_CONFIDENCE = 5      # §59: 5+ sucessos → alta confiança
DEFAULT_MAX_EVENTS = 2000         # §63 retenção
EVENT_TTL_SECONDS = 30 * 86400


def default_usage_home() -> Path:
    override = os.environ.get("KAZUMI_USAGE_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "KAZUMI" / "usage-learning"


def redact_arguments(arguments: dict[str, str]) -> dict[str, str]:
    """Mantém apenas chaves operacionais seguras; NUNCA conteúdo digitado."""
    safe_keys = {"filename", "force_new", "folder", "app"}
    return {k: v for k, v in arguments.items() if k in safe_keys}


class UsageEvent(BaseModel):
    """Schema kazumi-7c §53."""

    timestamp: float = Field(default_factory=time.time)
    context_signature: str = ""          # hash curto do contexto (não o conteúdo)
    intent: str = ""
    target: str = ""
    action_sequence: list[str] = Field(default_factory=list)
    capabilities_used: list[str] = Field(default_factory=list)
    verified_result: bool | None = None
    duration_ms: float = 0.0
    user_correction: bool = False        # §55: resolução anterior estava errada
    user_reversal: bool = False          # §60: usuário desfizer logo em seguida
    confidence: float = 1.0


class AliasStat(BaseModel):
    alias: str
    canonical: str
    kind: str = "app"
    successes: int = 0
    corrections: int = 0
    confidence: float = 0.5
    learned_because: str = "sucessos verificados consecutivos"
    last_confirmed: float = 0.0
    previous_canonical: str | None = None


class WorkflowCandidate(BaseModel):
    workflow_id: str
    steps: list[str]
    occurrences: int = 0
    success_count: int = 0
    failure_count: int = 0
    user_corrections: int = 0
    confidence: float = 0.3
    context_similarity: float = 0.8
    learned_because: str = "sequência recorrente verificada"
    last_confirmed: float = 0.0
    promoted_skill_id: str | None = None


class PreferenceStat(BaseModel):
    key: str                              # ex.: projeto:tarefa
    value: str                            # ex.: caminho/pasta (operacional, não privado)
    hits: int = 0
    confidence: float = 0.4
    learned_because: str = "associação repetida confirmada"
    last_confirmed: float = 0.0


class UsageLearningService:
    def __init__(self, base_dir: Path | None = None, *, clock: Callable = time.time,
                 max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self.base = base_dir or default_usage_home()
        self.clock = clock
        self.max_events = max_events
        self.events_path = self.base / "usage-events.jsonl"
        self.aliases_path = self.base / "aliases.json"
        self.preferences_path = self.base / "preferences.json"
        self.workflows_path = self.base / "workflow-candidates.json"
        self.aliases: dict[str, AliasStat] = {}
        self.preferences: dict[str, PreferenceStat] = {}
        self.workflows: dict[str, WorkflowCandidate] = {}
        self._sequence_window: dict[str, tuple[float, list[str]]] = {}
        self._load()

    # ------------------------------------------------------------ storage

    def _atomic_json(self, path: Path, data: Any) -> None:
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as error:
            logger.warning("usage_save_failed file=%s type=%s", path.name, type(error).__name__)

    def _load(self) -> None:
        def load_model_map(path: Path, model, attr: str) -> None:
            try:
                if path.is_file():
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    getattr(self, attr).clear()
                    for key, item in (raw.get("items") or {}).items():
                        getattr(self, attr)[key] = model.model_validate(item)
            except (OSError, ValueError) as error:
                logger.warning("usage_load_failed file=%s type=%s", path.name,
                               type(error).__name__)

        load_model_map(self.aliases_path, AliasStat, "aliases")
        load_model_map(self.preferences_path, PreferenceStat, "preferences")
        load_model_map(self.workflows_path, WorkflowCandidate, "workflows")

    def persist(self) -> None:
        self._atomic_json(self.aliases_path,
                          {"items": {k: v.model_dump() for k, v in self.aliases.items()}})
        self._atomic_json(self.preferences_path,
                          {"items": {k: v.model_dump() for k, v in self.preferences.items()}})
        self._atomic_json(self.workflows_path,
                          {"items": {k: v.model_dump() for k, v in self.workflows.items()}})

    # ------------------------------------------------------------- eventos

    def record(self, event: UsageEvent) -> None:
        """Append protegido + retenção (§63). Falha silenciosa nunca quebra chat."""
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.model_dump(), ensure_ascii=False)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self.compact()
        except OSError as error:
            logger.warning("usage_event_append_failed type=%s", type(error).__name__)

    def compact(self, force: bool = False) -> int:
        """Retenção: corta eventos antigos/redundantes mantendo agregados (§63)."""
        try:
            if not self.events_path.is_file():
                return 0
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
            if len(lines) < self.max_events and not force:
                return 0
            cutoff = self.clock() - EVENT_TTL_SECONDS
            kept = []
            seen_recent: set[str] = set()
            for line in lines[-self.max_events:]:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("timestamp", 0) < cutoff:
                    continue
                signature = f"{item.get('context_signature')}|{item.get('intent')}|{item.get('target')}"
                if signature in seen_recent and not item.get("user_correction"):
                    continue  # redundante: já está nos agregados
                seen_recent.add(signature)
                kept.append(line)
            tmp = self.events_path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.replace(tmp, self.events_path)
            return len(lines) - len(kept)
        except OSError:
            return 0

    def recent_events(self, limit: int = 50) -> list[dict]:
        try:
            if not self.events_path.is_file():
                return []
            lines = self.events_path.read_text(encoding="utf-8").splitlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError):
            return []

    # ------------------------------------------------------------- aliases

    def learn_alias_success(self, alias: str, canonical: str, kind: str = "app") -> AliasStat:
        """Sucesso VERIFICADO alimenta o alias (§54/§56)."""
        key = f"{kind}:{alias.casefold()}"
        stat = self.aliases.get(key) or AliasStat(alias=alias.casefold(),
                                                  canonical=canonical, kind=kind)
        stat.successes += 1
        stat.last_confirmed = self.clock()
        stat.confidence = min(0.99, 0.5 + 0.12 * stat.successes - 0.25 * stat.corrections)
        self.aliases[key] = stat
        self.persist()
        return stat

    def learn_alias_correction(self, alias: str, correct_canonical: str,
                               kind: str = "app") -> AliasStat:
        """Correção do usuário (§55): anterior errado, correto ganha confiança."""
        key = f"{kind}:{alias.casefold()}"
        stat = self.aliases.get(key) or AliasStat(alias=alias.casefold(),
                                                  canonical=correct_canonical, kind=kind)
        if stat.canonical.casefold() != correct_canonical.casefold():
            stat.previous_canonical = stat.canonical
            stat.canonical = correct_canonical
            # A correção explícita é a primeira confirmação do mapping certo;
            # sucessos do mapping errado não são transferidos.
            stat.successes = 1
        else:
            stat.successes += 1
        stat.corrections += 1
        stat.confidence = min(0.85, 0.5 + 0.12 * stat.successes)
        stat.learned_because = "correção explícita do operador"
        stat.last_confirmed = self.clock()
        self.aliases[key] = stat
        self.persist()
        return stat

    def resolve_alias(self, alias: str, kind: str = "app") -> str | None:
        stat = self.aliases.get(f"{kind}:{alias.casefold()}")
        if stat and stat.confidence >= 0.6 and stat.successes >= ALIAS_CONFIRM_THRESHOLD:
            return stat.canonical
        return None

    # --------------------------------------------------------- preferências

    def learn_preference(self, key: str, value: str) -> PreferenceStat:
        pref = self.preferences.get(key) or PreferenceStat(key=key, value=value)
        if pref.value == value:
            pref.hits += 1
        else:
            pref.value = value
            pref.hits = max(1, pref.hits // 2)
        pref.last_confirmed = self.clock()
        pref.confidence = min(0.95, 0.4 + 0.15 * pref.hits)
        self.preferences[key] = pref
        self.persist()
        return pref

    def resolve_preference(self, key: str) -> str | None:
        pref = self.preferences.get(key)
        if pref and pref.hits >= ALIAS_CONFIRM_THRESHOLD:
            return pref.value
        return None

    # ------------------------------------------------------------ workflows

    @staticmethod
    def context_signature(foreground_app: str | None, channel: str = "") -> str:
        raw = f"{(foreground_app or '').casefold()}|{channel}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def track_sequence_step(self, context_signature: str, step: str, *,
                            window_seconds: float = 600.0) -> list[str] | None:
        """Janela deslizante de dois passos por conversa/contexto.

        Manter o último passo permite detectar A→B em cada repetição de
        ``A,B,A,B`` sem fragmentar o padrão quando A/B mudam o foreground.
        """
        now = self.clock()
        current = self._sequence_window.get(context_signature)
        if current is None or now - current[0] > window_seconds:
            self._sequence_window[context_signature] = (now, [step])
            return None
        current[1].append(step)
        if len(current[1]) >= 2:
            completed = list(current[1][-2:])
            self._sequence_window[context_signature] = (now, [step])
            return completed
        self._sequence_window[context_signature] = (now, current[1])
        return None

    def record_workflow(self, context_signature: str, steps: list[str], *,
                        success: bool) -> WorkflowCandidate | None:
        """Agrega sequência verificada; cria candidate no threshold (§58/§59)."""
        if len(steps) < 2:
            return None
        workflow_id = hashlib.sha1(("|".join(steps)).encode()).hexdigest()[:12]
        candidate = self.workflows.get(workflow_id) or WorkflowCandidate(
            workflow_id=workflow_id, steps=list(dict.fromkeys(steps)))
        candidate.occurrences += 1
        if success:
            candidate.success_count += 1
        else:
            candidate.failure_count += 1
        candidate.last_confirmed = self.clock()
        ratio = candidate.success_count / max(1, candidate.occurrences)
        candidate.confidence = round(min(0.95, ratio * min(1.0, candidate.success_count / 5)), 2)
        self.workflows[workflow_id] = candidate
        self.persist()
        if candidate.success_count >= ALIAS_CONFIRM_THRESHOLD:
            return candidate
        return None

    def negative_workflow_signal(self, workflow_id: str) -> WorkflowCandidate | None:
        candidate = self.workflows.get(workflow_id)
        if candidate is None:
            return None
        candidate.user_corrections += 1
        candidate.confidence = round(max(0.05, candidate.confidence - 0.2), 2)
        candidate.learned_because += "; sinal negativo do operador"
        self.workflows[workflow_id] = candidate
        self.persist()
        return candidate

    # ------------------------------------------------------- explicabilidade

    @staticmethod
    def explain(stat: AliasStat | PreferenceStat | WorkflowCandidate) -> dict[str, Any]:
        return {
            "learned_because": stat.learned_because,
            "last_confirmed": stat.last_confirmed,
            "confidence": stat.confidence,
            "source_count": getattr(stat, "successes", None) or getattr(stat, "hits", None)
            or getattr(stat, "success_count", None),
        }
