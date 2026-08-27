"""Camada 3 — IntentUnderstandingService (nyra-7c §23-§31).

Transforma texto digitado OU transcrição STT em NormalizedUserIntent (§24).
Um único caminho para texto e voz (§25): ambos entram normalizados aqui.

Fast path determinístico (§26) reutiliza o parser do Universal Operator
(app.desktop.intents). Referências contextuais (§28) resolvem via
ComputerStateService. Comando composto canônico (§30) gera plano
estruturado de steps. Imperativos curtos com contexto suficiente (§31)
não caem em conversa genérica.

O LLM fica para ambiguidade real/planejamento (§77) — fora deste serviço.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from pydantic import BaseModel, Field

from app.computer.state import ComputerStateService, ResolvedTarget

logger = logging.getLogger("nyra.computer.intent")


class PlanStep(BaseModel):
    step: int
    capability: str          # open_app | type_text | save | close_app | verify_file ...
    target: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)


class NormalizedUserIntent(BaseModel):
    """Schema mínimo nyra-7c §24."""

    intent_id: str = Field(default_factory=lambda: f"int_{uuid4().hex[:12]}")
    turn_id: str = ""
    conversation_id: str = "default"
    action: str                       # OPEN_APP, CLOSE_APP, ... ou SKILL_RUN/PLAN/UNKNOWN
    target: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    desired_result: str = ""
    risk_hint: str = "LOW_RISK"
    confidence: float = 0.0
    requires_context: bool = False
    requires_perception: bool = False
    source_channel: str = "text"      # text | voice — só telemetria, executor é cego a isso
    plan: list[PlanStep] = Field(default_factory=list)
    resolved: ResolvedTarget | None = None
    # Alvo CRU para o Universal Operator executar (evita dupla resolução):
    raw_action: str = ""
    raw_target: str = ""
    raw_contextual: bool = False
    raw_explicit_new: bool = False


_DESIRED_BY_ACTION = {
    "OPEN_APP": "aplicativo aberto e utilizável",
    "CLOSE_APP": "aplicativo/janela fechada",
    "MINIMIZE_APP": "janela minimizada",
    "MAXIMIZE_APP": "janela maximizada",
    "RESTORE_APP": "janela restaurada",
    "FOCUS_APP": "janela em primeiro plano",
    "SWITCH_APP": "alvo em primeiro plano",
    "OPEN_FOLDER": "pasta aberta no Explorador",
    "OPEN_FILE": "arquivo aberto no aplicativo associado",
}

_BARE_VERBS = {
    "fecha": "CLOSE_APP", "feche": "CLOSE_APP", "fechar": "CLOSE_APP",
    "minimiza": "MINIMIZE_APP", "restaura": "RESTORE_APP", "maximiza": "MAXIMIZE_APP",
    "salva": "SAVE", "salve": "SAVE",
}


class IntentUnderstandingService:
    def __init__(self, state: ComputerStateService | None = None) -> None:
        self.state = state or ComputerStateService()
        self.last_failure_reason: str | None = None
        self.metrics: dict[str, float] = {
            "intent_parse_ms": 0.0,
            "context_resolve_ms": 0.0,
        }

    # ------------------------------------------------------------- pipeline

    def resolve(self, text: str, *, conversation_id: str = "default",
                turn_id: str | None = None,
                channel: str = "text") -> NormalizedUserIntent | None:
        started = time.perf_counter()
        self.last_failure_reason = None
        self.metrics["context_resolve_ms"] = 0.0
        try:
            return self._resolve_inner(text, conversation_id=conversation_id,
                                       turn_id=turn_id, channel=channel)
        finally:
            self.metrics["intent_parse_ms"] = round((time.perf_counter() - started) * 1000, 2)

    def _resolve_inner(self, text: str, *, conversation_id: str, turn_id: str | None,
                       channel: str) -> NormalizedUserIntent | None:
        from app.desktop.intents import (
            UniversalAction,
            parse_notepad_multistep,
            parse_universal_intent,
        )

        value = " ".join((text or "").strip().split())
        if not value or len(value) > 120:
            self.last_failure_reason = "invalid_input"
            return None

        # 1) composto canônico (§30): abrir→escrever→salvar
        multistep = parse_notepad_multistep(value)
        if multistep is not None:
            filename = multistep["filename"]
            close_after = bool(multistep.get("close_after"))
            plan = [
                PlanStep(step=1, capability="open_app", target="bloco de notas"),
                PlanStep(step=2, capability="verify_window", target="bloco de notas"),
                PlanStep(step=3, capability="type_text",
                         arguments={"text": multistep["text"]}),
                PlanStep(step=4, capability="save_file_as", target=filename),
                PlanStep(step=5, capability="verify_file",
                         arguments={"name": filename, "content": multistep["text"]}),
            ]
            if close_after:
                plan.append(PlanStep(step=6, capability="close_app",
                                     target="bloco de notas"))
            return NormalizedUserIntent(
                turn_id=turn_id or "", conversation_id=conversation_id,
                action="PLAN", target="bloco de notas",
                arguments={
                    "text": multistep["text"],
                    "filename": filename,
                    "close_after": "true" if close_after else "false",
                },
                desired_result=(f"bloco de notas com texto digitado e arquivo {filename} "
                                "salvo na área de trabalho" +
                                (" e janela fechada" if close_after else "")),
                confidence=0.95, source_channel=channel,
                plan=plan,
            )

        # 2) fast path determinístico existente (§26)
        parsed = parse_universal_intent(value)
        if parsed is None:
            resolved_bare = self._bare_imperative(
                value, conversation_id=conversation_id,
                turn_id=turn_id, channel=channel)
            if resolved_bare is None and self.last_failure_reason is None and \
                    self._looks_operational(value):
                self.last_failure_reason = "unrecognized"
            return resolved_bare

        requires_context = parsed.contextual
        resolved: ResolvedTarget | None = None
        target = parsed.target
        references: list[str] = []
        if parsed.contextual and self.state is not None:
            references.append(target)
            context_started = time.perf_counter()
            resolved = self.state.resolve_reference(
                target, conversation_id=conversation_id, turn_id=turn_id)
            self.metrics["context_resolve_ms"] = round(
                (time.perf_counter() - context_started) * 1000, 2)
            if resolved is not None:
                target = resolved.display_name
            else:
                self.last_failure_reason = "context_unresolved"
                return None  # sem contexto: NÃO inventa alvo (pergunta única vem depois)

        action_value = parsed.action.value if isinstance(parsed.action, UniversalAction) \
            else str(parsed.action)
        explicit_new = bool(getattr(parsed, "explicit_new", False))
        return NormalizedUserIntent(
            turn_id=turn_id or "", conversation_id=conversation_id,
            action=action_value, target=target,
            arguments={"force_new": "true"} if explicit_new else {},
            references=references,
            desired_result=_DESIRED_BY_ACTION.get(action_value, ""),
            confidence=1.0 if not requires_context else (0.9 if resolved else 0.4),
            requires_context=requires_context,
            requires_perception=False,
            source_channel=channel,
            resolved=resolved,
            raw_action=action_value,
            raw_target=parsed.target,
            raw_contextual=bool(parsed.contextual),
            raw_explicit_new=explicit_new,
        )

    # ------------------------------------------------- imperativos curtos §31

    def _bare_imperative(self, value: str, *, conversation_id: str, turn_id: str | None,
                         channel: str) -> NormalizedUserIntent | None:
        lowered = value.casefold()
        action = _BARE_VERBS.get(lowered.strip(" .!"))
        if action is None or self.state is None:
            return None
        context_started = time.perf_counter()
        resolved = self.state.resolve_reference(
            "ele", conversation_id=conversation_id, turn_id=turn_id)
        self.metrics["context_resolve_ms"] = round(
            (time.perf_counter() - context_started) * 1000, 2)
        if resolved is None:
            self.last_failure_reason = "context_unresolved"
            return None  # sem alvo no contexto: conversa/pergunta, não ação às cegas
        return NormalizedUserIntent(
            turn_id=turn_id or "", conversation_id=conversation_id,
            action=action, target=resolved.display_name,
            references=["ele"], desired_result=_DESIRED_BY_ACTION.get(action, ""),
            confidence=0.85, requires_context=True, source_channel=channel,
            resolved=resolved,
            raw_action=action,
            raw_target="ele",
            raw_contextual=True,
        )

    @staticmethod
    def _looks_operational(value: str) -> bool:
        lowered = value.casefold()
        return any(
            token in lowered.split()
            for token in (
                "abre", "abrir", "fecha", "fechar", "minimiza", "maximiza",
                "restaura", "traz", "foca", "salva", "digita", "escreve",
            )
        )
