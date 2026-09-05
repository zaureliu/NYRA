"""Deterministic creation, consolidation and natural-language routing policy."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata

from app.open_loops.models import OpenLoopState, OpenLoopType


_WORD = re.compile(r"[a-z0-9_-]{2,}")
_TRIVIAL = re.compile(
    r"^(?:oi|ola|opa|valeu|obrigad[oa]|brigad[oa]|ok|okay|beleza|bom dia|boa tarde|boa noite)[!. ]*$"
)
_PENDING_QUERY = re.compile(
    r"\b(?:o que|quais?|tem algo|algo)\b.{0,35}\b(?:pendente|aberto|faltando)\b|"
    r"\b(?:pendencias|open loops?)\b"
)
_RESUME_QUERY = re.compile(
    r"\b(?:retoma|retome|retomar|continua|continue|continuar|volta|volte|voltar)\b"
    r".{0,55}\b(?:aquilo|isso|onde parou|naquilo|naquela|naquele|configuracao|problema|atividade)?\b"
)
_STATUS_QUERY = re.compile(
    r"\b(?:terminou|resolveu|voltou|concluiu|ainda esta|e aquele|e aquela|como ficou)\b"
)
_WAITING = re.compile(r"\b(?:aguardando|esperando|assim que|quando .*?(?:terminar|voltar|ficar|chegar))\b")
_BLOCKED = re.compile(r"\b(?:bloquead[oa]|travou|impedid[oa]|nao consigo continuar|sem acesso)\b")
_FUTURE = re.compile(
    r"\b(?:depois (?:eu )?(?:testo|vejo|faco|continuo|confiro)|mais tarde|"
    r"precisamos? (?:retomar|continuar|voltar)|ainda (?:falta|nao|continua|esta|incomplet[oa]|pendente|ruim)|"
    r"ficou pendente|deixa para depois|retomar depois|interrompid[oa]|parcialmente conclu[ií]d[oa]|passo \d+)\b"
)

_ENTITY_ANCHORS = {
    "audio", "discord", "documentacao", "download", "kazumi", "release", "vm", "voz",
}

_STOPWORDS = {
    "a", "ao", "aos", "aquela", "aquele", "aquilo", "as", "com", "da", "das", "de",
    "depois", "do", "dos", "e", "ela", "ele", "em", "essa", "esse", "esta", "este",
    "eu", "fazer", "ficou", "mais", "na", "nao", "nas", "no", "nos", "o", "os", "para",
    "precisa", "precisamos", "que", "retomar", "testar", "testo", "um", "uma", "voltar",
    "ainda", "continua", "continuar", "corrigir", "resolver", "problema", "pendente",
}


def normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.casefold().split())


def topic_terms(value: str) -> set[str]:
    return {word for word in _WORD.findall(normalize(value)) if word not in _STOPWORDS}


def subject_key(title: str, project: str | None = None) -> str:
    terms = sorted(topic_terms(f"{title} {project or ''}"))
    return " ".join(terms[:20]) or normalize(title)[:120]


def consolidation_score(left_title: str, right_title: str, *, left_project: str | None,
                        right_project: str | None) -> float:
    if left_project and right_project and normalize(left_project) != normalize(right_project):
        return 0.0
    left, right = topic_terms(left_title), topic_terms(right_title)
    if not left or not right:
        lexical = 0.0
    else:
        lexical = len(left & right) / max(1, min(len(left), len(right)))
    sequence = SequenceMatcher(None, subject_key(left_title), subject_key(right_title)).ratio()
    anchor_bonus = .25 if left & right & _ENTITY_ANCHORS else 0.0
    return min(1.0, lexical * 0.75 + sequence * 0.25 + anchor_bonus)


def continuity_intent(text: str) -> tuple[OpenLoopType, OpenLoopState] | None:
    value = normalize(text)
    if not value or len(value) < 4 or _TRIVIAL.fullmatch(value):
        return None
    if is_pending_query(value) or is_resume_query(value) or is_status_query(value):
        return None
    if _BLOCKED.search(value):
        return OpenLoopType.BLOCKED_WORK, OpenLoopState.BLOCKED
    if _WAITING.search(value):
        return OpenLoopType.WAITING_CONDITION, OpenLoopState.WAITING
    if _FUTURE.search(value):
        kind = OpenLoopType.INTERRUPTED_WORK if "interrompid" in value or re.search(r"passo \d+", value) else OpenLoopType.PENDING_INTENTION
        return kind, OpenLoopState.OPEN
    return None


def is_pending_query(text: str) -> bool:
    return bool(_PENDING_QUERY.search(normalize(text)))


def is_resume_query(text: str) -> bool:
    return bool(_RESUME_QUERY.search(normalize(text)))


def is_status_query(text: str) -> bool:
    return bool(_STATUS_QUERY.search(normalize(text))) or normalize(text).endswith(" terminou?")
