from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.core.paths import IDENTITY_ROOT
from app.speech.pronunciation import PronunciationEngine, get_engine


CODE_BLOCK = re.compile(r"```(?:\w+)?\s*.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`]+)`")
MARKDOWN_LINK = re.compile(r"\[([^]]+)]\([^)]+\)")
RAW_URL = re.compile(r"https?://\S+")
WINDOWS_PATH = re.compile(r"(?<!\w)(?:[A-Za-z]:\\|\\\\)[^\s,;]+")
POSIX_PATH = re.compile(r"(?<!\w)/(?:[\w.-]+/)+[\w.-]+")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
LIST_MARKER = re.compile(r"^\s*(?:[-*+] |\d+[.)]\s+)", re.MULTILINE)
EMPHASIS = re.compile(r"(?<!\w)[*_~]{1,3}|[*_~]{1,3}(?!\w)")
PERCENT = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*%")
DURATION = re.compile(r"\b(\d+)d\s+(\d+)h\b", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?])\s+")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\uFE0F]")
EMOTION_MARKER = re.compile(
    r"(?:<\/?emotion\b[^>]*>|<emotion\s*=\s*[^>]+>|^\s*\[(?:emotion|style)\s*:[^]]+]\s*)",
    re.IGNORECASE | re.MULTILINE,
)
INTERNAL_TRACE = re.compile(
    r"^\s*(?:TOOL|TRACE|METADATA|SYSTEM|RUNTIME|AGENT)(?:_[A-Z0-9_]+|\s+(?:RESULT|TRACE|METADATA))?\s*[:=].*$",
    re.MULTILINE | re.IGNORECASE,
)
INTERNAL_IDENTIFIER = re.compile(
    r"\b(?:PID|HWND|MONITOR(?:_ID)?|DISPLAY_ID|WINDOW_HANDLE)\s*[:=#]?\s*(?:0x[0-9a-f]+|[a-z0-9_-]{2,})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PreparedSpeech:
    display_text: str
    speech_text: str
    chunks: tuple[str, ...]
    applied_rules: tuple[dict, ...] = ()


class PronunciationDictionary:
    def __init__(self, path=None) -> None:
        source = path or IDENTITY_ROOT / "pronunciation_ptbr.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        self.terms: dict[str, str] = data.get("terms", {})
        ordered = sorted(self.terms, key=len, reverse=True)
        self.pattern = re.compile(
            r"(?<![\w-])(" + "|".join(re.escape(term) for term in ordered) + r")(?![\w-])",
            re.IGNORECASE,
        )
        self._casefold = {key.casefold(): value for key, value in self.terms.items()}

    def apply(self, text: str) -> str:
        return self.pattern.sub(lambda match: self._casefold[match.group(0).casefold()], text)


class SpeechTextNormalizer:
    def __init__(self, pronunciation: PronunciationDictionary | None = None, engine: PronunciationEngine | None = None) -> None:
        self.pronunciation = pronunciation or PronunciationDictionary()
        self._legacy_override = pronunciation is not None
        self.engine = engine or get_engine()

    def prepare(self, display_text: str, max_chunk_chars: int = 280, provider: str = "default", literal_required: bool = False) -> PreparedSpeech:
        text = display_text.replace("\r\n", "\n")
        text = EMOTION_MARKER.sub("", text)
        text = INTERNAL_TRACE.sub("", text)
        text = INTERNAL_IDENTIFIER.sub("identificador interno omitido", text)
        try:
            structured = json.loads(text)
            if isinstance(structured, (dict, list)):
                text = "Detalhes estruturados disponÃ­veis na tela."
        except (json.JSONDecodeError, TypeError):
            pass
        text = CODE_BLOCK.sub(" Código disponível na tela. ", text)
        text = MARKDOWN_LINK.sub(r"\1", text)
        text = RAW_URL.sub("endereço disponível na tela", text)
        text = WINDOWS_PATH.sub("caminho disponível na tela", text)
        text = POSIX_PATH.sub("caminho disponível na tela", text)
        text = INLINE_CODE.sub(r"\1", text)
        text = HEADING.sub("", text)
        text = LIST_MARKER.sub("", text)
        text = EMPHASIS.sub("", text)
        text = EMOJI.sub("", text)
        text = text.replace("|", ", ").replace("→", "para").replace("=>", "para")
        text = DURATION.sub(lambda m: f"{m.group(1)} dias e {m.group(2)} horas", text)
        if self._legacy_override:
            text = self.pronunciation.apply(text)
        pronunciation = self.engine.prepare_for_speech(text, provider=provider, literal_required=literal_required)
        text = pronunciation.speech_text
        if self._legacy_override:
            text = self.pronunciation.apply(text)
        text = PERCENT.sub(lambda m: f"{self._number(m.group(1))} por cento", text)
        text = re.sub(r"\b(\d+)\b", lambda m: self._number(m.group(1)), text)
        text = re.sub(r"\s*[/\\]\s*", " barra ", text)
        text = re.sub(r"[<>\[\]{}]", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\s*\n\s*", "\n\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        chunks = tuple(self._chunks(text, max_chunk_chars))
        return PreparedSpeech(display_text=display_text, speech_text="\n\n".join(chunks), chunks=chunks, applied_rules=tuple(pronunciation.applied_rules))

    @staticmethod
    def _number(value: str) -> str:
        normalized = value.replace(",", ".")
        try:
            from num2words import num2words

            number = float(normalized) if "." in normalized else int(normalized)
            return str(num2words(number, lang="pt_BR"))
        except (ValueError, TypeError, NotImplementedError):
            return value

    @staticmethod
    def _chunks(text: str, maximum: int) -> list[str]:
        output: list[str] = []
        for paragraph in text.split("\n\n"):
            current = ""
            sentences = SENTENCE.split(paragraph.strip())
            for sentence in sentences:
                if not sentence:
                    continue
                if len(current) + len(sentence) + 1 <= maximum:
                    current = f"{current} {sentence}".strip()
                    continue
                if current:
                    output.append(current)
                while len(sentence) > maximum:
                    split_at = sentence.rfind(",", 0, maximum)
                    if split_at < maximum // 2:
                        split_at = sentence.rfind(" ", 0, maximum)
                    if split_at <= 0:
                        split_at = maximum
                    output.append(sentence[:split_at].strip().rstrip(",") + ".")
                    sentence = sentence[split_at:].lstrip(" ,")
                current = sentence
            if current:
                output.append(current)
        return output or [""]


class ProsodyProcessor(SpeechTextNormalizer):
    """Backward-compatible name for the V1/V3 call sites."""
