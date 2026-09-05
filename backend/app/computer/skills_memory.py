r"""Camada 7 — SkillMemoryService (kazumi-7c §64-§74).

Transforma workflows CONFIRMADOS (UsageLearning) em skills reutilizáveis.
Skill é definição ESTRUTURADA (§65), não prompt livre.

Ciclo: workflow candidate ≥3 sucessos → skill CANDIDATE → promoção p/
LEARNED (ou "aprende isso" explícito, §68). Execução: match →
precondições → step → verify → next (§70). Falha repetida degrada e faz
fallback para o planner normal (§72). Versioning runtime simples (§71).

Storage: %LOCALAPPDATA%\KAZUMI\skills (§74), sem secrets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger("kazumi.computer.skills")


class SkillState(StrEnum):
    CANDIDATE = "CANDIDATE"
    LEARNED = "LEARNED"
    DEPRECATED = "DEPRECATED"
    DISABLED = "DISABLED"


class LearnedStep(BaseModel):
    capability: str            # open_app | close_app | focus_app | open_folder | open_file | wait
    target: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)


class LearnedSkill(BaseModel):
    skill_id: str = Field(default_factory=lambda: f"skl_{uuid4().hex[:10]}")
    name: str                  # slug curto, ex.: open_kazumi_workspace
    aliases: list[str] = Field(default_factory=list)
    trigger_intents: list[str] = Field(default_factory=list)
    preconditions: list[dict[str, str]] = Field(default_factory=list)  # {"kind":"app_visible","value":"code"}
    steps: list[LearnedStep] = Field(default_factory=list)
    verification: str = "cada passo verificado na fonte determinística"
    failure_recovery: str = "1 retry alternativo; falhou → degrada e cai no planner"
    confidence: float = 0.5
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_success: float = 0.0
    last_failure: float = 0.0
    version: int = 1
    state: SkillState = SkillState.CANDIDATE
    degraded: bool = False
    source_workflow_id: str | None = None
    learned_because: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)  # versões anteriores


PROMOTE_MIN_CONFIDENCE = 0.6
DEGRADE_THRESHOLD_CONFIDENCE = 0.35


def default_skills_home() -> Path:
    override = os.environ.get("KAZUMI_SKILLS_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "KAZUMI" / "skills"


def register_skill_events() -> None:
    from app.events import EventType

    additions = {
        "SKILL_CANDIDATE_CREATED": "skill.candidate.created",
        "SKILL_LEARNED": "skill.learned",
        "SKILL_EXECUTED": "skill.executed",
        "SKILL_DEGRADED": "skill.degraded",
        "USAGE_PATTERN_DETECTED": "usage.pattern.detected",
    }
    for name, value in additions.items():
        if not hasattr(EventType, name):
            setattr(EventType, name, value)


register_skill_events()


class SkillExecutionReport(BaseModel):
    skill_id: str
    matched_by: str = ""
    ok: bool = False
    steps: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""


class SkillMemoryService:
    def __init__(self, base_dir: Path | None = None, *, clock: Callable = time.time,
                 event_bus=None) -> None:
        self.base = base_dir or default_skills_home()
        self.store_path = self.base / "skills.json"
        self.clock = clock
        self.event_bus = event_bus
        self.skills: dict[str, LearnedSkill] = {}
        self._load()

    # ------------------------------------------------------------ storage

    def _load(self) -> None:
        try:
            if self.store_path.is_file():
                raw = json.loads(self.store_path.read_text(encoding="utf-8"))
                for item in raw.get("skills", []):
                    skill = LearnedSkill.model_validate(item)
                    self.skills[skill.skill_id] = skill
        except (OSError, ValueError) as error:
            logger.warning("skills_load_failed type=%s", type(error).__name__)

    def persist(self) -> None:
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1,
                       "skills": [s.model_dump() for s in self.skills.values()]}
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.store_path)
        except OSError as error:
            logger.warning("skills_save_failed type=%s", type(error).__name__)

    # ---------------------------------------------------------- criação

    @staticmethod
    def _slug(name_hint: str) -> str:
        import re

        slug = re.sub(r"[^a-z0-9_]+", "_", name_hint.casefold()).strip("_")
        return slug[:48] or f"skill_{uuid4().hex[:6]}"

    def from_workflow_candidate(self, candidate, alias_hint: str | None = None) -> LearnedSkill:
        steps = [self._step_from_usage(step) for step in candidate.steps]
        skill = LearnedSkill(
            name=self._slug(alias_hint or "_".join(candidate.steps)[:40]),
            aliases=[alias_hint.casefold()] if alias_hint else [],
            trigger_intents=[step.casefold() for step in candidate.steps],
            steps=steps,
            confidence=min(0.75, candidate.confidence),
            state=SkillState.CANDIDATE,
            source_workflow_id=candidate.workflow_id,
            learned_because=candidate.learned_because,
        )
        self.skills[skill.skill_id] = skill
        candidate.promoted_skill_id = skill.skill_id
        self.persist()
        self._publish("SKILL_CANDIDATE_CREATED", skill_id=skill.skill_id, name=skill.name,
                      confidence=skill.confidence)
        return skill

    @staticmethod
    def _step_from_usage(step: str) -> LearnedStep:
        action, separator, target = step.partition(":")
        exact = {
            "OPEN_APP": "open_app",
            "CLOSE_APP": "close_app",
            "MINIMIZE_APP": "minimize_app",
            "MAXIMIZE_APP": "maximize_app",
            "RESTORE_APP": "restore_app",
            "FOCUS_APP": "focus_app",
            "SWITCH_APP": "focus_app",
            "OPEN_FOLDER": "open_folder",
            "OPEN_FILE": "open_file",
        }
        capability = exact.get(action.upper()) if separator else None
        if capability is not None and target.strip():
            return LearnedStep(capability=capability, target=target.strip())

        # Compatibilidade com candidatos antigos em linguagem natural.
        lowered = step.casefold()
        if any(word in lowered for word in ("abriu", "abre", "open")) and \
                any(word in lowered for word in ("pasta", "folder")):
            capability = "open_folder"
        elif any(word in lowered for word in ("abriu", "abre", "open")) and \
                any(word in lowered for word in ("arquivo", "file")):
            capability = "open_file"
        elif any(word in lowered for word in ("fechou", "fecha", "close")):
            capability = "close_app"
        elif any(word in lowered for word in ("focou", "traz", "focus", "foreground")):
            capability = "focus_app"
        elif any(word in lowered for word in ("minimizou", "minimiza")):
            capability = "minimize_app"
        elif any(word in lowered for word in ("abriu", "abre", "open")):
            capability = "open_app"
        else:
            capability = "unsupported"
        return LearnedStep(capability=capability, target=target.strip() or step.strip())

    def explicit_learn(self, steps: list[tuple[str, str]], name_hint: str,
                       aliases: list[str] | None = None) -> LearnedSkill:
        """'aprende isso' (§68): sequência única vira LEARNED imediato."""
        slug = self._slug(name_hint)
        existing = next((s for s in self.skills.values() if s.name == slug), None)
        new_steps = [LearnedStep(capability=cap, target=target) for cap, target in steps]
        if existing is not None:
            # §71/§73: mesma skill re-aprendida vira nova versão, não duplicata.
            if existing.steps != new_steps:
                existing.history.append({"version": existing.version,
                                         "steps": [s.model_dump() for s in existing.steps]})
                existing.version += 1
                existing.steps = new_steps
            existing.aliases = sorted({*existing.aliases,
                                       *[a.casefold() for a in (aliases or [])]})
            existing.state = SkillState.LEARNED
            existing.degraded = False
            existing.last_success = self.clock()
            self.persist()
            return existing
        skill = LearnedSkill(
            name=slug,
            aliases=[a.casefold() for a in (aliases or [])],
            steps=new_steps,
            confidence=0.85,
            state=SkillState.LEARNED,
            learned_because="comando explícito do operador ('aprende isso')",
            last_success=self.clock(),
        )
        self.skills[skill.skill_id] = skill
        self.persist()
        self._publish("SKILL_LEARNED", skill_id=skill.skill_id, name=skill.name,
                      source="explicit")
        return skill

    def promote(self, skill_id: str, *, force: bool = False) -> LearnedSkill | None:
        skill = self.skills.get(skill_id)
        if skill is None:
            return None
        if not force and skill.confidence < PROMOTE_MIN_CONFIDENCE:
            return None
        skill.state = SkillState.LEARNED
        skill.last_success = skill.last_success or self.clock()
        self.persist()
        self._publish("SKILL_LEARNED", skill_id=skill.skill_id, name=skill.name,
                      confidence=skill.confidence)
        return skill

    # ------------------------------------------------------------- matching

    def match(self, text: str) -> tuple[LearnedSkill, str] | None:
        lowered = f" {text.casefold().strip()} "
        best: tuple[LearnedSkill, str] | None = None
        best_score = 0
        for skill in self.skills.values():
            if skill.state != SkillState.LEARNED or skill.degraded:
                continue
            for trigger in (*skill.aliases, *skill.trigger_intents, skill.name.replace("_", " ")):
                token = f" {trigger.casefold().strip()} "
                if token in lowered and len(token) > best_score:
                    best, best_score = (skill, f"alias:{trigger}"), len(token)
        return best

    def get(self, skill_id: str) -> LearnedSkill | None:
        return self.skills.get(skill_id)

    def list_skills(self, include_candidates: bool = True) -> list[dict[str, Any]]:
        out = []
        for skill in self.skills.values():
            if not include_candidates and skill.state != SkillState.LEARNED:
                continue
            data = skill.model_dump()
            data["explain"] = {
                "learned_because": skill.learned_because,
                "last_success": skill.last_success,
                "confidence": skill.confidence,
                "source_count": skill.success_count,
            }
            out.append(data)
        return out

    # ------------------------------------------------------------ execução

    async def execute(self, skill: LearnedSkill, controller=None,
                      verifier=None, *, turn_id: str | None = None) -> SkillExecutionReport:
        """§70: preconditions → step → verify → next. NUNCA sucesso cego."""
        report = SkillExecutionReport(skill_id=skill.skill_id, matched_by="explicit")
        if controller is None:
            report.message = "controller indisponível"
            return report

        # Precondições (§70): se falham, NÃO executa steps stale.
        for precondition in skill.preconditions:
            kind = precondition.get("kind")
            value = precondition.get("value", "")
            if kind == "app_visible" and verifier is not None:
                effect = await asyncio.to_thread(verifier.verify_app_visible, value)
                if not effect.verified:
                    report.message = (
                        f"precondição falhou: {value} não está visível — nada foi executado."
                    )
                    self._degrade(skill, precondition_failed=True)
                    return report
            elif kind in {"file_exists", "folder_exists"}:
                from pathlib import Path

                target = Path(value)
                exists = target.is_file() if kind == "file_exists" else target.is_dir()
                if not exists:
                    report.message = (
                        f"precondição falhou: {value} não existe — nada foi executado."
                    )
                    self._degrade(skill, precondition_failed=True)
                    return report
            elif kind not in {"app_visible", "file_exists", "folder_exists"}:
                report.message = (
                    f"precondição desconhecida ({kind}) — nada foi executado."
                )
                self._degrade(skill, precondition_failed=True)
                return report

        from app.desktop.intents import UniversalIntent, UniversalAction

        step_reports = report.steps
        all_ok = True
        for index, step in enumerate(skill.steps, start=1):
            intent = self._intent_for_step(step)
            if intent is None:
                step_reports.append({
                    "step": index, "capability": step.capability,
                    "target": step.target, "reply": "capability não suportada",
                    "verified": False,
                })
                all_ok = False
                break
            handled, reply = await controller.handle_universal(intent, turn_id=turn_id)
            effect = None
            if verifier is not None and getattr(controller, "last_operation_result", None):
                effect = verifier.from_operation_result(
                    controller.last_operation_result,
                    expected=f"{step.capability}:{step.target}")
            entry = {"step": index, "capability": step.capability, "target": step.target,
                     "reply": reply, "verified": bool(effect.verified) if effect else None}
            step_reports.append(entry)
            if not handled or effect is None or effect.verified is not True:
                all_ok = False
                break

        skill.usage_count += 1
        if all_ok and step_reports:
            skill.success_count += 1
            skill.last_success = self.clock()
            skill.confidence = min(0.97, skill.confidence + 0.05)
            report.ok = True
            report.message = f"{len(step_reports)} passo(s) executado(s) e verificado(s)."
        elif step_reports:
            skill.failure_count += 1
            skill.last_failure = self.clock()
            self._degrade(skill)
            report.message = f"falhou no passo {len(step_reports)}; confiança reduzida."
        self.persist()
        self._publish("SKILL_EXECUTED", skill_id=skill.skill_id, ok=report.ok)
        return report

    @staticmethod
    def _intent_for_step(step: LearnedStep):
        from app.desktop.intents import UniversalAction, UniversalIntent

        mapping = {
            "open_app": UniversalAction.OPEN_APP,
            "close_app": UniversalAction.CLOSE_APP,
            "minimize_app": UniversalAction.MINIMIZE_APP,
            "maximize_app": UniversalAction.MAXIMIZE_APP,
            "restore_app": UniversalAction.RESTORE_APP,
            "focus_app": UniversalAction.FOCUS_APP,
            "open_folder": UniversalAction.OPEN_FOLDER,
            "open_file": UniversalAction.OPEN_FILE,
        }
        action = mapping.get(step.capability)
        if action is None:
            return None
        contextual = step.target in {"ele", "ela"}
        return UniversalIntent(action=action, target=step.target, contextual=contextual)

    # ----------------------------------------------------------- degradação

    def _degrade(self, skill: LearnedSkill, *, precondition_failed: bool = False) -> None:
        if precondition_failed:
            skill.failure_count += 1
            skill.last_failure = self.clock()
        skill.confidence = round(max(0.02, skill.confidence - 0.2), 2)
        if skill.confidence < DEGRADE_THRESHOLD_CONFIDENCE:
            skill.degraded = True
            if skill.state == SkillState.LEARNED:
                skill.state = SkillState.DEPRECATED
        self.persist()
        self._publish("SKILL_DEGRADED", skill_id=skill.skill_id,
                      confidence=skill.confidence, degraded=skill.degraded)

    def record_user_correction(self, skill_id: str) -> None:
        """Correção do operador derruba confiança sem apagar a definição (§60/§73)."""
        skill = self.skills.get(skill_id)
        if skill is None:
            return
        if "correção do operador" not in skill.learned_because:
            skill.learned_because = (skill.learned_because + "; correção do operador").strip("; ")
        self._degrade(skill)

    def new_version(self, skill_id: str, steps: list[tuple[str, str]]) -> LearnedSkill | None:
        """§71: variação recorrente → v2 preservando rollback da v1."""
        skill = self.skills.get(skill_id)
        if skill is None:
            return None
        skill.history.append({"version": skill.version,
                              "steps": [s.model_dump() for s in skill.steps]})
        skill.version += 1
        skill.steps = [LearnedStep(capability=cap, target=target) for cap, target in steps]
        skill.degraded = False
        self.persist()
        return skill

    def _publish(self, event_name: str, **payload) -> None:
        if self.event_bus is None:
            return
        try:
            import asyncio

            from app.events import EventType

            payload.setdefault("source", "skill_memory")
            coroutine = self.event_bus.publish(getattr(EventType, event_name), **payload)
            task = asyncio.ensure_future(coroutine)
            del task  # fire-and-forget protegido pelo bus
        except Exception:  # noqa: BLE001
            pass
