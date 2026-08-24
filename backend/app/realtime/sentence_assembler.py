from __future__ import annotations

import re
import time


_BOUNDARY = re.compile(r"(?<=[!?…])(?:[\"'”’)]*)\s+|(?<=\.)(?:[\"'”’)]*)\s+")
_PROTECTED = re.compile(
    r"(?ix)(?:https?://\S+|www\.\S+|(?:\d{1,3}\.){3}\d{1,3}|"
    r"\bv?\d+(?:\.\d+){1,3}\b|\b(?:sr|sra|srta|dr|dra|prof|etc|ex|obs|vs)\.)"
)


class SentenceAssembler:
    """Turns an incremental LLM stream into natural, ordered speech units."""

    def __init__(self, minimum_characters: int = 28, minimum_words: int = 4, timeout_ms: int = 850) -> None:
        self.minimum_characters = minimum_characters
        self.minimum_words = minimum_words
        self.timeout_seconds = timeout_ms / 1000
        self.buffer = ""
        self._updated_at = time.monotonic()

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self.buffer += chunk
        self._updated_at = time.monotonic()
        return self._extract_complete()

    def flush_due(self, now: float | None = None) -> list[str]:
        if not self.buffer.strip() or (now or time.monotonic()) - self._updated_at < self.timeout_seconds:
            return []
        if not self._speakable(self.buffer):
            return []
        value, self.buffer = self.buffer.strip(), ""
        return [value]

    def finish(self) -> list[str]:
        complete = self._extract_complete(force=True)
        if self.buffer.strip():
            complete.append(self.buffer.strip())
            self.buffer = ""
        return complete

    def _extract_complete(self, force: bool = False) -> list[str]:
        protected, replacements = self._protect(self.buffer)
        boundaries = list(_BOUNDARY.finditer(protected))
        if not boundaries:
            return []
        output: list[str] = []
        consumed = 0
        pending = ""
        for match in boundaries:
            part = self._restore(protected[consumed:match.end()].strip(), replacements)
            consumed = match.end()
            pending = f"{pending} {part}".strip()
            if self._speakable(pending) or force:
                output.append(pending)
                pending = ""
        remainder = self._restore(protected[consumed:], replacements)
        self.buffer = f"{pending} {remainder}".lstrip()
        return output

    def _speakable(self, value: str) -> bool:
        return len(value.strip()) >= self.minimum_characters and len(value.split()) >= self.minimum_words

    @staticmethod
    def _protect(value: str) -> tuple[str, list[str]]:
        replacements: list[str] = []
        def replace(match: re.Match[str]) -> str:
            replacements.append(match.group(0))
            return f"\uE000{len(replacements) - 1}\uE001"
        return _PROTECTED.sub(replace, value), replacements

    @staticmethod
    def _restore(value: str, replacements: list[str]) -> str:
        for index, original in enumerate(replacements):
            value = value.replace(f"\uE000{index}\uE001", original)
        return value
