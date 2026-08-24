from __future__ import annotations

import re

EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\uFE0F]")
OPENING_FILLER = re.compile(r"^\s*(?:claro|com certeza|certamente)[!,.\s]+", re.IGNORECASE)
EMPTY_CLOSERS = (
    re.compile(r"\s*Se precisar de algo,? (?:estou|fico) (?:aqui )?para ajudar\.?", re.IGNORECASE),
    re.compile(r"\s*Se quiser,? posso ajudar a (?:verificar|testar) [^.?!]+[.?!]", re.IGNORECASE),
    re.compile(r"\s*O que (?:você )?(?:deseja|quer) (?:verificar|testar|fazer)(?: ou (?:verificar|testar|fazer))?\?", re.IGNORECASE),
    re.compile(r"\s*Como posso ajudar(?: você)?\?", re.IGNORECASE),
)


def apply_response_style(text: str) -> str:
    """Remove presentation filler without changing technical statements."""
    value = OPENING_FILLER.sub("", EMOJI.sub("", text))
    for pattern in EMPTY_CLOSERS:
        value = pattern.sub("", value)
    return re.sub(r"[ \t]{2,}", " ", value).strip()
