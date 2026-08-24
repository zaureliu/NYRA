from __future__ import annotations

import re
from time import perf_counter

from .lexicon import load_dictionary
from .models import PronunciationResult, PronunciationRule
from .numbers import normalize_numbers

URL = re.compile(r"https?://\S+|\b[\w.-]+\.(?:com|net|org|io|dev)(?:/\S*)?", re.I)
PATH = re.compile(r"(?:[A-Za-z]:\\[^\s]+|/api/[\w./-]+)")
MARKDOWN = re.compile(r"[*_~#>`]")


class PronunciationEngine:
    """Deterministic, local pronunciation preparation. Never executes or translates commands."""

    def __init__(self) -> None:
        self.dictionary = load_dictionary()
        self._compiled: list[tuple[PronunciationRule, re.Pattern[str]]] = []
        for rule in sorted(self.dictionary.rules, key=lambda item: (item.priority, len(item.canonical)), reverse=True):
            if not rule.enabled:
                continue
            aliases = sorted(set([rule.canonical, *rule.aliases]), key=len, reverse=True)
            pattern = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(alias) for alias in aliases) + r")(?![\w-])", re.I)
            self._compiled.append((rule, pattern))

    def reload(self) -> None:
        self.__init__()

    def prepare_for_speech(self, text: str, provider: str = "default", locale: str = "pt-BR", literal_required: bool = False) -> PronunciationResult:
        started = perf_counter()
        original = text
        normalized = text
        warnings: list[str] = []
        applied: list[dict] = []
        detected: list[str] = []
        protected: dict[str, str] = {}
        spoken_protected: dict[str, str] = {}
        if not literal_required:
            normalized = URL.sub("endereço disponível na tela", normalized)
            normalized = PATH.sub("caminho disponível na tela", normalized)
        else:
            def hold(match: re.Match) -> str:
                key = f"§LITERAL{len(protected)}§"
                protected[key] = match.group(0)
                return key
            normalized = URL.sub(hold, normalized)
            normalized = PATH.sub(hold, normalized)
        normalized = MARKDOWN.sub("", normalized)
        normalized, number_rules = normalize_numbers(normalized)
        applied.extend(number_rules)
        for rule, pattern in self._compiled:
            def replace(match: re.Match, current_rule: PronunciationRule = rule) -> str:
                detected.append(current_rule.canonical)
                strategy = current_rule.provider_overrides.get(provider, current_rule.strategy)
                spoken = current_rule.provider_overrides.get(provider) or current_rule.spoken_form or current_rule.canonical
                if strategy == "provider_native":
                    spoken = current_rule.canonical
                elif strategy == "spell_letters":
                    spoken = " ".join(current_rule.canonical)
                elif strategy == "expand" and current_rule.spoken_form:
                    spoken = current_rule.spoken_form
                applied.append({"term": match.group(0), "canonical": current_rule.canonical, "strategy": strategy, "spoken_form": spoken, "source": "default"})
                key = f"§TERM{len(spoken_protected)}§"
                spoken_protected[key] = spoken
                return key
            normalized = pattern.sub(replace, normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        for key, value in spoken_protected.items():
            normalized = normalized.replace(key, value)
        for key, value in protected.items():
            normalized = normalized.replace(key, value)
        if not normalized:
            warnings.append("speech_text vazio após normalização")
            normalized = original.strip()
        result = PronunciationResult(original_text=original, normalized_text=normalized, speech_text=normalized, applied_rules=applied, detected_terms=list(dict.fromkeys(detected)), warnings=warnings)
        result.warnings.append(f"engine_ms={round((perf_counter() - started) * 1000, 3)}")
        return result


_ENGINE: PronunciationEngine | None = None


def get_engine() -> PronunciationEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = PronunciationEngine()
    return _ENGINE


def reload_engine() -> PronunciationEngine:
    global _ENGINE
    _ENGINE = PronunciationEngine()
    return _ENGINE
