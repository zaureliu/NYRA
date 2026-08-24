from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod

from app.listening.models import WakeWordMatch


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


class WakeWordProvider(ABC):
    """Local wake-word abstraction.

    The initial provider works on the local faster-whisper transcript.  An acoustic
    model can replace it later without changing the session manager or API.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def detect(self, text: str, wake_word: str) -> WakeWordMatch: ...


class TranscriptWakeWordProvider(WakeWordProvider):
    @property
    def name(self) -> str:
        return "local_transcript"

    def detect(self, text: str, wake_word: str) -> WakeWordMatch:
        folded_text = _fold(text)
        folded_wake = _fold(wake_word)
        aliases = {folded_wake}
        if folded_wake == "nyra":
            # Common pt-BR Whisper spellings, intentionally narrow to reduce false wakes.
            aliases.update({"nira", "naira"})
        pattern = re.compile(rf"(?:^|\b)(?:{'|'.join(map(re.escape, sorted(aliases, key=len, reverse=True)))})\b", re.IGNORECASE)
        match = pattern.search(folded_text)
        if not match:
            return WakeWordMatch(detected=False, wake_word=wake_word)
        # Accents do not change offsets for these aliases; remove only a leading call.
        command = folded_text[match.end():].lstrip(" ,.:;!?-—")
        if match.start() > 12:
            # Mid-sentence mentions are not activation calls.
            return WakeWordMatch(detected=False, wake_word=wake_word)
        return WakeWordMatch(
            detected=True,
            wake_word=wake_word,
            command_text=command,
            confidence=1.0,
        )
