from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import logging
import time
from uuid import uuid4

logger = logging.getLogger("kazumi.conversation.session")


@dataclass
class VoiceTurn:
    turn_id: str
    response_id: str
    user_text: str
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    generated_text: str = ""
    spoken_text: str = ""
    cancelled_text: str = ""
    interrupted: bool = False
    emotion: str = "neutral"
    modality: str = "voice"
    chunks: dict[int, str] = field(default_factory=dict)
    played: set[int] = field(default_factory=set)
    partial_chunk: int | None = None
    spoken_fraction: float | None = None
    marks: dict[str, float] = field(default_factory=dict)


class ConversationSession:
    """Bounded, volatile context. Generation is never evidence of playback."""

    def __init__(self) -> None:
        self.conversation_id = f"voice_session_{uuid4().hex}"
        self.session_started_at = self.last_activity_at = time.time()
        self.turns: deque[VoiceTurn] = deque(maxlen=40)
        self.user_speaking = False
        self.playing_response: str | None = None
        self.pending_tool_runs: set[str] = set()
        self.closed = False
        self.barge_in_samples: deque[float] = deque(maxlen=100)
        logger.info("conversation_session_started session_id=%s", self.conversation_id)

    def find(self, response_id: str | None) -> VoiceTurn | None:
        return next((t for t in reversed(self.turns) if response_id in (t.response_id, t.turn_id)), None)

    def begin(self, turn, speech_end: float | None = None) -> VoiceTurn:
        value = VoiceTurn(turn.turn_id, turn.response_id, turn.user_input)
        value.marks["stt_final"] = time.perf_counter()
        if speech_end is not None:
            value.marks["speech_end"] = speech_end
        self.turns.append(value)
        self.user_speaking = False
        self.last_activity_at = time.time()
        logger.info("turn_finalized session_id=%s turn_id=%s", self.conversation_id, turn.turn_id)
        return value

    def interrupt(self, response_id: str | None) -> None:
        value = self.find(response_id)
        if value:
            value.interrupted = True
            value.cancelled_text = value.generated_text[len(value.spoken_text):].strip() if value.generated_text.startswith(value.spoken_text) else value.generated_text
            value.marks.setdefault("barge_in", time.perf_counter())
        logger.info("barge_in session_id=%s", self.conversation_id)

    def playback(self, payload) -> str | None:
        """Return only a newly completed whole chunk for the existing memory policy.

        A timed partial does NOT imply exact words: retain its fraction separately.
        The caller cannot supply text, emotion or generated content in an ack.
        """
        value = self.find(payload.response_id)
        if payload.playing:
            self.playing_response = payload.response_id
        elif self.playing_response == payload.response_id or payload.response_id is None:
            self.playing_response = None
        self.last_activity_at = time.time()
        if not value:
            return None
        if payload.phase == "started":
            value.marks.setdefault("first_audio", time.perf_counter())
            logger.info("first_audio turn_id=%s", value.turn_id)
        if payload.phase == "interrupted":
            self.interrupt(payload.response_id)
            value.partial_chunk = payload.chunk_index
            value.spoken_fraction = payload.spoken_fraction
            if payload.barge_in_latency_ms is not None:
                self.barge_in_samples.append(payload.barge_in_latency_ms)
        if payload.phase == "completed" and payload.chunk_index in value.chunks and payload.chunk_index not in value.played:
            value.played.add(payload.chunk_index)
            value.spoken_text = " ".join(value.chunks[i] for i in sorted(value.played))
            value.cancelled_text = " ".join(text for i, text in sorted(value.chunks.items()) if i not in value.played) if value.interrupted else ""
            return value.chunks[payload.chunk_index]
        return None

    def context(self) -> str:
        lines = ["Sessão de voz contínua. Responda naturalmente e concisamente, mantendo Persona/Dialogue Policy. "
                 "Texto gerado não prova que foi ouvido. USER CLAIM nunca é observação de hardware."]
        for turn in list(self.turns)[-6:-1]:
            lines.append(f"Usuário: {turn.user_text[:800]}")
            lines.append(f"Fala confirmada pelo player: {turn.spoken_text[:1000] or '(nenhuma confirmação)'}")
            if turn.interrupted:
                lines.append("Resposta interrompida. Não suponha que o restante foi ouvido; acolha a correção atual.")
        return "\n".join(lines)

    @staticmethod
    def distribution(values) -> dict:
        values = sorted(values)
        if not values:
            return {"count": 0, "average_ms": None, "p50_ms": None, "p95_ms": None}
        return {"count": len(values), "average_ms": round(sum(values) / len(values), 2),
                "p50_ms": round(values[round((len(values)-1)*.5)], 2),
                "p95_ms": round(values[round((len(values)-1)*.95)], 2)}

    def snapshot(self) -> dict:
        metrics = {}
        for label, start, end in (
            ("VAD_END_TO_STT_FINAL", "speech_end", "stt_final"),
            ("STT_FINAL_TO_LLM_FIRST_TOKEN", "stt_final", "first_token"),
            ("LLM_FIRST_TOKEN_TO_FIRST_TTS_REQUEST", "first_token", "tts_request"),
            ("TTS_REQUEST_TO_FIRST_AUDIO", "tts_request", "first_audio"),
            ("USER_SPEECH_END_TO_FIRST_AUDIO", "speech_end", "first_audio"),
        ):
            metrics[label] = self.distribution((t.marks[end]-t.marks[start])*1000 for t in self.turns if start in t.marks and end in t.marks)
        metrics["BARGE_IN_DETECTION_TO_PLAYBACK_STOP"] = self.distribution(self.barge_in_samples)
        states = [] if self.closed else ["LISTENING"]
        if self.user_speaking: states.append("USER_SPEAKING")
        if self.playing_response: states.append("ASSISTANT_SPEAKING")
        if any(t.ended_at is None for t in self.turns): states.append("PROCESSING")
        if self.pending_tool_runs: states.append("TOOL_RUNNING")
        latest = self.turns[-1] if self.turns else None
        return {"conversation_id": self.conversation_id, "session_started_at": self.session_started_at,
                "last_activity_at": self.last_activity_at, "states": states or ["IDLE"],
                "current_turn": latest.turn_id if latest else None, "user_speaking": self.user_speaking,
                "assistant_speaking": bool(self.playing_response), "interrupted": bool(latest and latest.interrupted),
                "current_emotion": latest.emotion if latest else None, "pending_tool_runs": sorted(self.pending_tool_runs),
                "metrics": metrics, "turn_count": len(self.turns), "closed": self.closed}

    def transcript(self) -> list[dict]:
        return [{**asdict(t), "played": sorted(t.played)} for t in self.turns]

    def close(self) -> None:
        self.closed = True
        self.playing_response = None
        self.user_speaking = False
        self.pending_tool_runs.clear()
        logger.info("conversation_session_closed session_id=%s", self.conversation_id)
