from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpeechTimestamp:
    text: str
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class AudioPacket:
    """Provider-neutral incremental audio; no invented word alignment."""
    pcm: bytes = b""
    sample_rate: int = 24000
    path: Path | None = None
    timestamps: tuple[SpeechTimestamp, ...] = ()

    def __post_init__(self):
        if self.sample_rate not in (16000, 24000, 48000) or len(self.pcm) % 2:
            raise ValueError("Invalid mono S16LE audio packet")
        if len(self.pcm) > 96000:
            raise ValueError("Audio packet exceeds bounded transport size")
