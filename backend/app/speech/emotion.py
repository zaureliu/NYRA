from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
import re
import time
from typing import Any, Mapping


class VoiceEmotion(StrEnum):
    NEUTRAL = "neutral"
    FRIENDLY = "friendly"
    FOCUSED = "focused"
    CONFIDENT = "confident"
    POSITIVE = "positive"
    HAPPY = "happy"
    RELIEVED = "relieved"
    CONCERNED = "concerned"
    WARNING = "warning"
    SERIOUS = "serious"
    EMPATHETIC = "empathetic"
    CURIOUS = "curious"
    SURPRISED = "surprised"
    AMUSED = "amused"
    APOLOGETIC = "apologetic"
    UNCERTAIN = "uncertain"
    CALM = "calm"


VALID_EMOTIONS = frozenset(item.value for item in VoiceEmotion)


class Expressiveness(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class VoiceIdentity:
    identity_id: str = "KAZUMI_VOICE_AVA_V1"
    language: str = "pt-BR"
    presentation: str = "adult_young_feminine"
    register: str = "medium_feminine"
    personality: str = "intelligent, calm, attentive, confident, natural"
    synthetic_identity: bool = True


KAZUMI_VOICE = VoiceIdentity()


@dataclass(frozen=True, slots=True)
class EmotionPlan:
    emotion: VoiceEmotion = VoiceEmotion.NEUTRAL
    intensity: float = 0.2
    confidence: float = 0.5
    style_instruction: str = ""
    reason: str = "default"
    turn_id: str | None = None
    sentence_index: int | None = None

    @classmethod
    def validated(
        cls,
        emotion: str | VoiceEmotion,
        intensity: float,
        *,
        confidence: float = 0.5,
        reason: str = "validated",
        turn_id: str | None = None,
        sentence_index: int | None = None,
    ) -> "EmotionPlan":
        try:
            selected = VoiceEmotion(str(emotion).casefold())
        except ValueError:
            selected = VoiceEmotion.NEUTRAL
            reason = "unknown_emotion_fallback"
        try:
            level = min(0.65, max(0.0, float(intensity)))
        except (TypeError, ValueError):
            level = 0.2
        return cls(
            emotion=selected,
            intensity=round(level, 3),
            confidence=min(1.0, max(0.0, float(confidence))),
            style_instruction=style_instruction(selected, level),
            reason=reason,
            turn_id=turn_id,
            sentence_index=sentence_index,
        )


_STYLE_INSTRUCTIONS: dict[VoiceEmotion, str] = {
    VoiceEmotion.NEUTRAL: "Speak naturally and conversationally in Brazilian Portuguese.",
    VoiceEmotion.FRIENDLY: "Speak warmly and naturally, with an open conversational tone.",
    VoiceEmotion.FOCUSED: "Speak with clear diction, stable rhythm, and restrained emotion.",
    VoiceEmotion.CONFIDENT: "Speak with calm confidence and a firm, natural sentence ending.",
    VoiceEmotion.POSITIVE: "Speak with subtly positive energy, without sounding performative.",
    VoiceEmotion.HAPPY: "Speak with restrained happiness and a subtle natural smile.",
    VoiceEmotion.RELIEVED: "Speak with gentle relief and a gradually more relaxed cadence.",
    VoiceEmotion.CONCERNED: "Speak carefully with mild concern and slightly longer pauses.",
    VoiceEmotion.WARNING: "Speak firmly and clearly, a little slower, without shouting.",
    VoiceEmotion.SERIOUS: "Speak seriously with controlled rhythm and no playful inflection.",
    VoiceEmotion.EMPATHETIC: "Speak gently and considerately, without theatrical emotion.",
    VoiceEmotion.CURIOUS: "Speak with a natural investigative, lightly questioning intonation.",
    VoiceEmotion.SURPRISED: "Speak with brief, restrained surprise, avoiding exaggeration.",
    VoiceEmotion.AMUSED: "Speak lightly amused with a subtle smile, without artificial laughter.",
    VoiceEmotion.APOLOGETIC: "Speak with a careful, sincere apology and restrained energy.",
    VoiceEmotion.UNCERTAIN: "Speak cautiously with a natural non-conclusive cadence.",
    VoiceEmotion.CALM: "Speak calmly at a comfortable pace with clear articulation.",
}


def style_instruction(emotion: VoiceEmotion, intensity: float) -> str:
    base = _STYLE_INSTRUCTIONS[emotion]
    return f"{base} Emotional intensity: {min(0.65, max(0.0, intensity)):.2f}. Keep the same speaker identity."


@dataclass(frozen=True, slots=True)
class _Candidate:
    emotion: VoiceEmotion
    intensity: float
    confidence: float
    priority: int
    reason: str


class EmotionPlanner:
    """Deterministic semantic planner with turn ownership and mild hysteresis.

    Structured operation metadata outranks language cues. The planner never
    inserts tags into speech text and never invents an engine capability.
    """

    _RISK = re.compile(r"\b(apagar|desligar|remover|excluir|formatar|reiniciar|irrevers[ií]vel|risco)\b", re.I)
    _FAILURE = re.compile(r"\b(falhou|erro|offline|indispon[ií]vel|corrompid[oa]|cr[ií]tic[oa])\b", re.I)
    _SUCCESS = re.compile(r"\b(pronto|conclu[ií]d[oa]|funcionou|online|resolvid[oa]|voltou ao normal|sucesso)\b", re.I)
    _RECOVERY = re.compile(r"\b(finalmente|recuperad[oa]|restaurad[oa]|voltou|normalizad[oa])\b", re.I)
    _SELF_ERROR = re.compile(r"\b(desculp[ae]|eu (?:errei|falhei|interpretei).{0,40}(?:errad|mal))\b", re.I)
    _UNCERTAIN = re.compile(r"\b(talvez|pode ser|provavelmente|ainda n[aã]o (?:confirmei|sei)|n[aã]o tenho certeza)\b", re.I)
    _INVESTIGATION = re.compile(r"\b(verificando|investigando|analisando|diagn[oó]stico|evid[eê]ncia|hip[oó]tese)\b", re.I)
    _GREETING = re.compile(r"^\s*(?:oi|ol[aá]|bom dia|boa tarde|boa noite)\b", re.I)
    _AMUSEMENT = re.compile(r"\b(engra[cç]ad|ir[oô]nic|essa foi inesperada|quase suspeito)\b|(?:rs|kkk|haha)", re.I)
    _SURPRISE = re.compile(r"\b(u[eé]|inesperad[oa]|sozinh[oa]|como assim)\b", re.I)
    _DELICATE = re.compile(r"\b(sinto muito|lamento|delicad[oa]|dif[ií]cil|cuidado)\b", re.I)

    def __init__(self, *, expressiveness: str = "normal", emotion_mode: str = "automatic") -> None:
        self.expressiveness = Expressiveness(expressiveness)
        self.emotion_mode = emotion_mode if emotion_mode in {"automatic", "neutral_only"} else "automatic"
        self._current = EmotionPlan.validated("neutral", 0.2)
        self._hold_sentences = 0
        self._last_change = 0.0
        self._turn_plans: dict[str, list[EmotionPlan]] = {}
        self._active_turn_id: str | None = None

    def configure(self, *, expressiveness: str | None = None, emotion_mode: str | None = None) -> None:
        if expressiveness is not None:
            try:
                self.expressiveness = Expressiveness(expressiveness)
            except ValueError:
                self.expressiveness = Expressiveness.NORMAL
        if emotion_mode is not None:
            self.emotion_mode = emotion_mode if emotion_mode in {"automatic", "neutral_only"} else "automatic"

    def plan(
        self,
        user_text: str,
        response_text: str,
        *,
        context: Mapping[str, Any] | None = None,
        turn_id: str | None = None,
        sentence_index: int | None = None,
    ) -> EmotionPlan:
        if self.emotion_mode == "neutral_only":
            result = EmotionPlan.validated("neutral", 0.0, confidence=1.0, reason="neutral_only", turn_id=turn_id, sentence_index=sentence_index)
            return self._record(result)
        # Continuity applies inside one streamed answer. A new user turn is a
        # real context boundary and must be able to move from success to error
        # (or the reverse) without inheriting a sentence-level hold.
        if turn_id is None or turn_id != self._active_turn_id:
            self._hold_sentences = 0
            self._active_turn_id = turn_id
        data = dict(context or {})
        combined = f"{user_text}\n{response_text}".strip()
        candidate = self._classify(user_text, response_text, combined, data)
        intensity = candidate.intensity * {Expressiveness.LOW: 0.72, Expressiveness.NORMAL: 1.0, Expressiveness.HIGH: 1.18}[self.expressiveness]
        proposed = EmotionPlan.validated(
            candidate.emotion,
            intensity,
            confidence=candidate.confidence,
            reason=candidate.reason,
            turn_id=turn_id,
            sentence_index=sentence_index,
        )
        return self._record(self._stabilize(proposed, candidate.priority))

    def dominant(self, turn_id: str | None, fallback: EmotionPlan | None = None) -> EmotionPlan:
        plans = self._turn_plans.pop(turn_id or "", [])
        if not plans:
            return fallback or self._current
        counts = Counter(plan.emotion for plan in plans)
        emotion = counts.most_common(1)[0][0]
        matching = [plan for plan in plans if plan.emotion == emotion]
        return max(matching, key=lambda item: item.confidence)

    def cancel_turn(self, turn_id: str | None) -> None:
        self._turn_plans.pop(turn_id or "", None)

    def _record(self, plan: EmotionPlan) -> EmotionPlan:
        if plan.turn_id:
            self._turn_plans.setdefault(plan.turn_id, []).append(plan)
            if len(self._turn_plans) > 64:
                self._turn_plans.pop(next(iter(self._turn_plans)))
        return plan

    def _stabilize(self, proposed: EmotionPlan, priority: int) -> EmotionPlan:
        current = self._current
        force = priority >= 80 or proposed.emotion in {VoiceEmotion.WARNING, VoiceEmotion.SERIOUS, VoiceEmotion.APOLOGETIC}
        if proposed.emotion != current.emotion and self._hold_sentences > 0 and not force and proposed.confidence < current.confidence + 0.18:
            self._hold_sentences -= 1
            return EmotionPlan.validated(
                current.emotion,
                min(current.intensity, proposed.intensity + 0.1),
                confidence=current.confidence,
                reason="emotion_continuity",
                turn_id=proposed.turn_id,
                sentence_index=proposed.sentence_index,
            )
        if proposed.emotion != current.emotion:
            # Avoid a caricature jump at a sentence boundary.
            if current.emotion != VoiceEmotion.NEUTRAL:
                proposed = EmotionPlan.validated(
                    proposed.emotion,
                    min(proposed.intensity, current.intensity + 0.15),
                    confidence=proposed.confidence,
                    reason=proposed.reason,
                    turn_id=proposed.turn_id,
                    sentence_index=proposed.sentence_index,
                )
            self._hold_sentences = 1 if priority < 80 else 0
            self._last_change = time.monotonic()
        self._current = proposed
        return proposed

    def _classify(self, user: str, response: str, combined: str, data: Mapping[str, Any]) -> _Candidate:
        severity = str(data.get("severity") or "").casefold()
        outcome = str(data.get("outcome") or "").casefold()
        source = str(data.get("error_source") or "").casefold()
        message_type = str(data.get("message_type") or "").casefold()
        destructive = bool(data.get("destructive")) or bool(self._RISK.search(combined))
        if source == "kazumi" or bool(data.get("kazumi_error")) or self._SELF_ERROR.search(response):
            return _Candidate(VoiceEmotion.APOLOGETIC, 0.38, 0.94, 100, "kazumi_error")
        if destructive or message_type == "confirmation_required":
            return _Candidate(VoiceEmotion.WARNING, 0.55, 0.95, 95, "destructive_or_risk")
        if severity in {"critical", "high"}:
            return _Candidate(VoiceEmotion.SERIOUS, 0.52, 0.94, 90, "structured_severity")
        if outcome in {"recovered", "restored"} or (self._RECOVERY.search(response) and self._SUCCESS.search(response)):
            return _Candidate(VoiceEmotion.RELIEVED, 0.38, 0.88, 75, "recovery")
        if outcome in {"failed", "error"} or self._FAILURE.search(response):
            return _Candidate(VoiceEmotion.CONCERNED, 0.38, 0.84, 72, "failure")
        if outcome in {"success", "completed"}:
            return _Candidate(VoiceEmotion.CONFIDENT, 0.34, 0.9, 70, "structured_success")
        if self._UNCERTAIN.search(response) or bool(data.get("uncertain")):
            return _Candidate(VoiceEmotion.UNCERTAIN, 0.3, 0.86, 68, "uncertainty")
        if self._SURPRISE.search(response) or bool(data.get("unexpected")):
            return _Candidate(VoiceEmotion.SURPRISED, 0.36, 0.82, 65, "unexpected_result")
        if self._DELICATE.search(response) or message_type == "delicate":
            return _Candidate(VoiceEmotion.EMPATHETIC, 0.3, 0.78, 62, "delicate_context")
        if self._AMUSEMENT.search(combined) and severity not in {"critical", "high"}:
            return _Candidate(VoiceEmotion.AMUSED, 0.3, 0.76, 55, "light_humor")
        if self._INVESTIGATION.search(combined) or bool(data.get("technical")):
            emotion = VoiceEmotion.CURIOUS if response.rstrip().endswith("?") else VoiceEmotion.FOCUSED
            return _Candidate(emotion, 0.27, 0.78, 50, "investigation")
        if self._SUCCESS.search(response):
            emotion = VoiceEmotion.HAPPY if len(response) < 40 and "!" in response else VoiceEmotion.POSITIVE
            return _Candidate(emotion, 0.34, 0.74, 45, "positive_result")
        if self._GREETING.search(user) or message_type == "greeting":
            return _Candidate(VoiceEmotion.FRIENDLY, 0.25, 0.72, 35, "greeting")
        if len(response) > 420 or message_type == "instruction":
            return _Candidate(VoiceEmotion.CALM, 0.23, 0.66, 25, "long_explanation")
        if response.rstrip().endswith("?"):
            return _Candidate(VoiceEmotion.CURIOUS, 0.25, 0.64, 20, "question")
        return _Candidate(VoiceEmotion.NEUTRAL, 0.2, 0.58, 10, "ordinary_response")


EMOTION_FALLBACKS: dict[VoiceEmotion, VoiceEmotion] = {
    VoiceEmotion.AMUSED: VoiceEmotion.FRIENDLY,
    VoiceEmotion.RELIEVED: VoiceEmotion.POSITIVE,
    VoiceEmotion.CONCERNED: VoiceEmotion.SERIOUS,
    VoiceEmotion.SURPRISED: VoiceEmotion.CURIOUS,
    VoiceEmotion.EMPATHETIC: VoiceEmotion.CALM,
    VoiceEmotion.APOLOGETIC: VoiceEmotion.CALM,
    VoiceEmotion.UNCERTAIN: VoiceEmotion.CALM,
}
