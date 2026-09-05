"""Deterministic emotion and dialogue policies; no second language model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.events import Event, EventType
from app.persona_runtime.models import DialogueMode, DialoguePolicy, KazumiEmotion


@dataclass(frozen=True, slots=True)
class EmotionSignal:
    emotion: KazumiEmotion
    intensity: float
    confidence: float
    priority: int
    reason: str
    half_life_seconds: float = 900.0
    max_restore_age_seconds: float = 21600.0


_JOKE = re.compile(r"\b(?:kkk+|haha+|rsrs+|piada|brincadeira|tô zoando|to zoando)\b", re.I)
_JOKE_REQUEST = re.compile(r"\b(?:conte|conta|manda)\s+(?:uma\s+)?piada\b", re.I)
_TECHNICAL = re.compile(
    r"\b(?:erro|falha|bug|log|stack|rede|dns|servidor|vm|código|codigo|api|banco|diagn[oó]st|investig)\b",
    re.I,
)
_EXECUTE = re.compile(r"^\s*(?:abre|abra|fecha|feche|cria|crie|executa|execute|instala|instale|reinicia|reinicie)\b", re.I)
_EXPLAIN = re.compile(r"\b(?:explica|explique|como funciona|por que|qual a diferença)\b", re.I)
_CONFIRM = re.compile(r"\b(?:confirma|tem certeza|posso prosseguir|autoriza|aprova)\b", re.I)
_CHALLENGE = re.compile(r"\b(?:discordo|isso está errado|isso esta errado|não faz sentido|nao faz sentido)\b", re.I)
_META_IDENTITY = re.compile(r"\b(?:quem (?:é|e) voc[eê]|sua identidade|sua personalidade|agora voc[eê] (?:é|e))\b", re.I)
_META_VOICE = re.compile(r"\b(?:sua voz|tts|fala mais|tom de voz|voice)\b", re.I)
_GREETING = re.compile(r"^\s*(?:oi|ol[aá]|opa|bom dia|boa tarde|boa noite)\b", re.I)


class DialoguePolicyEngine:
    def for_turn(self, text: str, *, emotion: KazumiEmotion = KazumiEmotion.NEUTRAL) -> DialoguePolicy:
        value = text.strip()
        if _META_IDENTITY.search(value):
            return self._policy(DialogueMode.META_IDENTITY, "identity_question")
        if _META_VOICE.search(value):
            return self._policy(DialogueMode.META_VOICE, "voice_question")
        if _CONFIRM.search(value):
            return self._policy(DialogueMode.CONFIRM, "confirmation", grounding=True)
        if _EXECUTE.search(value):
            return self._policy(DialogueMode.EXECUTE, "action_request", grounding=True)
        if _CHALLENGE.search(value):
            return self._policy(DialogueMode.CHALLENGE, "operator_challenge")
        if _JOKE_REQUEST.search(value):
            return self._policy(DialogueMode.JOKE, "joke_request", humor=True)
        if _JOKE.search(value):
            return self._policy(DialogueMode.PLAYFUL_REPLY, "user_joking", humor=True)
        if _EXPLAIN.search(value):
            return self._policy(DialogueMode.EXPLAIN, "explanation_request")
        if _TECHNICAL.search(value):
            return self._policy(DialogueMode.TECHNICAL_DIAGNOSIS, "technical_context", grounding=True)
        if _GREETING.search(value):
            return self._policy(DialogueMode.CASUAL_CHAT, "casual_greeting", humor=emotion == KazumiEmotion.AMUSED)
        if value.endswith("?"):
            return self._policy(DialogueMode.ASK, "question")
        return self._policy(DialogueMode.INFORM, "ordinary_turn")

    def for_event(self, event_name: str, payload: dict[str, Any] | None = None) -> DialoguePolicy:
        data = payload or {}
        normalized = event_name.upper()
        state = str(data.get("state") or data.get("status") or data.get("outcome") or "").upper()
        if normalized in {"CRITICAL_FAILURE", "DANGEROUS_ACTION"} or state == "CRITICAL":
            return self._policy(DialogueMode.WARN, "critical_failure", grounding=True)
        if normalized == "TASK_FAILED":
            return self._policy(DialogueMode.WARN, "operation_failure", grounding=True)
        if normalized == "KAZUMI_ERROR":
            return self._policy(DialogueMode.APOLOGIZE, "kazumi_error")
        if normalized in {"OPERATION_SUCCESS", "TASK_SUCCEEDED", "SYSTEM_RECOVERED"} or state in {"SUCCEEDED", "COMPLETED"}:
            return self._policy(DialogueMode.REPORT_RESULT, "verified_result", grounding=True)
        if state in {"FAILED", "BLOCKED", "ERROR"}:
            return self._policy(DialogueMode.WARN, "operation_failure", grounding=True)
        return self._policy(DialogueMode.INFORM, "structured_event", grounding=True)

    @staticmethod
    def _policy(mode: DialogueMode, reason: str, *, humor: bool = False,
                grounding: bool = False) -> DialoguePolicy:
        directness = "gentle" if mode == DialogueMode.APOLOGIZE else "direct"
        depth = "deep" if mode == DialogueMode.TECHNICAL_DIAGNOSIS else "adaptive"
        return DialoguePolicy(
            mode=mode, directness=directness, technical_depth=depth,
            humor_allowed=humor, requires_grounding=grounding, reason=reason,
        )


def user_emotion_signal(text: str) -> EmotionSignal:
    if _JOKE.search(text):
        return EmotionSignal(KazumiEmotion.AMUSED, .40, .88, 55, "USER_JOKING", 600, 3600)
    if _TECHNICAL.search(text):
        return EmotionSignal(KazumiEmotion.FOCUSED, .32, .82, 48, "TECHNICAL_CONTEXT", 1200, 21600)
    if _GREETING.search(text):
        return EmotionSignal(KazumiEmotion.FRIENDLY, .26, .78, 25, "NORMAL_CHAT", 900, 7200)
    if text.rstrip().endswith("?"):
        return EmotionSignal(KazumiEmotion.CURIOUS, .25, .68, 20, "QUESTION", 900, 7200)
    return EmotionSignal(KazumiEmotion.NEUTRAL, .16, .62, 10, "NORMAL_CHAT", 600, 3600)


def event_emotion_signal(event: Event) -> EmotionSignal | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    state = str(payload.get("state") or payload.get("status") or payload.get("outcome") or "").upper()

    if event.type in {EventType.SHELL_APPROVAL_REQUIRED, EventType.REMOTE_SHELL_APPROVAL_REQUIRED}:
        return EmotionSignal(KazumiEmotion.WARNING, .55, .96, 100, "DANGEROUS_ACTION", 1200, 21600)
    if event.type == EventType.ERROR:
        own_error = str(payload.get("operation") or "").casefold() in {"chat", "llm", "tts", "pipeline"}
        emotion = KazumiEmotion.APOLOGETIC if own_error else KazumiEmotion.CONCERNED
        return EmotionSignal(emotion, .42, .93, 95, "KAZUMI_ERROR" if own_error else "SYSTEM_ERROR", 1200, 21600)
    if event.type in {EventType.RUNTIME_CRASH_LOOP}:
        return EmotionSignal(KazumiEmotion.SERIOUS, .58, .98, 100, "CRITICAL_FAILURE", 1800, 21600)
    if event.type in {EventType.RUNTIME_FAILED, EventType.PROXMOX_TASK_FAILED,
                      EventType.MONITOR_JOB_FAILED, EventType.SELFDEV_VALIDATION_FAIL,
                      EventType.COMPUTER_OPERATOR_FAILURE, EventType.COMPUTER_VERIFICATION_FAILURE}:
        return EmotionSignal(KazumiEmotion.CONCERNED, .43, .92, 85, "TASK_FAILED", 1500, 21600)
    if event.type in {EventType.TASK_STATE_CHANGED, EventType.TASK_FINISHED,
                      EventType.AGENT_RUN_FINISHED, EventType.JOB_FINISHED,
                      EventType.WORKFLOW_FINISHED}:
        if state in {"FAILED", "BLOCKED", "ERROR"}:
            return EmotionSignal(KazumiEmotion.CONCERNED, .43, .92, 85, "TASK_FAILED", 1500, 21600)
        if state in {"SUCCEEDED", "COMPLETED"}:
            return EmotionSignal(KazumiEmotion.CONFIDENT, .36, .92, 70, "TASK_SUCCEEDED", 1200, 21600)
    if event.type in {EventType.RUNTIME_RECOVERED, EventType.NETWORK_RECOVERED,
                      EventType.NETWORK_GATEWAY_RECOVERED, EventType.NETWORK_INTERNET_RECOVERED,
                      EventType.NETWORK_DNS_RECOVERED, EventType.NETWORK_LINK_UP,
                      EventType.HOMELAB_HOST_ONLINE, EventType.MONITOR_JOB_COMPLETED}:
        return EmotionSignal(KazumiEmotion.RELIEVED, .38, .90, 75, "SYSTEM_RECOVERED", 900, 7200)
    if event.type in {EventType.PROXMOX_TASK_COMPLETED, EventType.SELFDEV_VALIDATION_PASS,
                      EventType.SELFDEV_POST_VALIDATION_PASS, EventType.COMPUTER_EFFECT_VERIFIED}:
        return EmotionSignal(KazumiEmotion.CONFIDENT, .34, .88, 68, "TASK_SUCCEEDED", 1200, 21600)
    if event.type in {EventType.SHELL_EXECUTION_FINISHED, EventType.REMOTE_SHELL_EXECUTION_FINISHED}:
        if payload.get("effect_verified") is True:
            return EmotionSignal(KazumiEmotion.CONFIDENT, .34, .9, 70, "TASK_SUCCEEDED", 1200, 21600)
        if payload.get("success") is False or payload.get("ok") is False:
            return EmotionSignal(KazumiEmotion.CONCERNED, .4, .88, 82, "TASK_FAILED", 1500, 21600)
    if bool(payload.get("unexpected")):
        return EmotionSignal(KazumiEmotion.SURPRISED, .38, .80, 60, "UNEXPECTED_RESULT", 300, 1800)
    return None


def named_emotion_signal(event_name: str) -> EmotionSignal | None:
    """Canonical semantic mapping for producers that already normalized events."""
    return {
        "TASK_SUCCEEDED": EmotionSignal(KazumiEmotion.CONFIDENT, .36, .92, 70, "TASK_SUCCEEDED", 1200, 21600),
        "TASK_FAILED": EmotionSignal(KazumiEmotion.CONCERNED, .43, .92, 85, "TASK_FAILED", 1500, 21600),
        "SYSTEM_RECOVERED": EmotionSignal(KazumiEmotion.RELIEVED, .38, .90, 75, "SYSTEM_RECOVERED", 900, 7200),
        "DANGEROUS_ACTION": EmotionSignal(KazumiEmotion.WARNING, .55, .96, 100, "DANGEROUS_ACTION", 1200, 21600),
        "UNEXPECTED_RESULT": EmotionSignal(KazumiEmotion.SURPRISED, .38, .80, 60, "UNEXPECTED_RESULT", 300, 1800),
        "USER_JOKING": EmotionSignal(KazumiEmotion.AMUSED, .40, .88, 55, "USER_JOKING", 600, 3600),
        "KAZUMI_ERROR": EmotionSignal(KazumiEmotion.APOLOGETIC, .42, .93, 95, "KAZUMI_ERROR", 1200, 21600),
        "NORMAL_CHAT": EmotionSignal(KazumiEmotion.FRIENDLY, .26, .78, 25, "NORMAL_CHAT", 900, 7200),
        "OPERATION_SUCCESS": EmotionSignal(KazumiEmotion.CONFIDENT, .34, .90, 70, "TASK_SUCCEEDED", 1200, 21600),
        "CRITICAL_FAILURE": EmotionSignal(KazumiEmotion.SERIOUS, .58, .98, 100, "CRITICAL_FAILURE", 1800, 21600),
    }.get(event_name.upper())
