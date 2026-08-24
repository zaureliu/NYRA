from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any


@dataclass
class _Turn:
    response_id: str
    started: float = field(default_factory=time.perf_counter)
    marks: dict[str, float] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict)


class RealtimeTelemetry:
    """In-memory timing only; conversation text is deliberately excluded."""

    def __init__(self, history_size: int = 200) -> None:
        self._turns: dict[str, _Turn] = {}
        self._completed_bases: dict[str, float] = {}
        self._timeline: deque[dict[str, Any]] = deque(maxlen=history_size)
        self.last_metrics: dict[str, float | str | None] = {}

    def start(self, response_id: str, speech_end: float | None = None) -> None:
        turn = self._turns.get(response_id) or _Turn(response_id=response_id)
        if speech_end is not None:
            turn.marks["t_user_speech_end"] = speech_end
        self._turns[response_id] = turn
        self.record("USER_SPEECH_FINAL", response_id=response_id)

    def active(self, response_id: str) -> bool:
        return response_id in self._turns

    def playback_started(self, response_id: str) -> None:
        if response_id in self._turns:
            self.mark(response_id, "t_playback_start")
            return
        base = self._completed_bases.get(response_id)
        if base is not None and self.last_metrics.get("response_id") == response_id:
            elapsed = round((time.perf_counter() - base) * 1000, 1)
            self.last_metrics["playback_start_ms"] = elapsed
            self.last_metrics["speech_to_playback_ms"] = elapsed
            self.record("PLAYBACK_START", response_id=response_id, milliseconds=elapsed)

    def mark(self, response_id: str, name: str, **safe: Any) -> float:
        now = time.perf_counter()
        turn = self._turns.setdefault(response_id, _Turn(response_id=response_id))
        turn.marks.setdefault(name, now)
        self.record(name.removeprefix("t_").upper(), response_id=response_id, **safe)
        return now

    def measure(self, response_id: str, name: str, milliseconds: float) -> None:
        turn = self._turns.setdefault(response_id, _Turn(response_id=response_id))
        turn.values[name] = round(float(milliseconds), 1)

    def finish(self, response_id: str) -> dict[str, float | str | None]:
        turn = self._turns.pop(response_id, None)
        if turn is None:
            return {}
        marks = turn.marks
        end = marks.get("t_response_complete", time.perf_counter())
        base = marks.get("t_user_speech_end", turn.started)
        self._completed_bases[response_id] = base
        if len(self._completed_bases) > 200:
            self._completed_bases.pop(next(iter(self._completed_bases)))
        def delta(later: str, earlier: str | None = None) -> float | None:
            left = marks.get(later)
            right = marks.get(earlier, base) if earlier else base
            return round((left - right) * 1000, 1) if left is not None and right is not None else None
        stt_total = delta("t_stt_final", "t_user_speech_end")
        first_token = delta("t_llm_first_token", "t_ollama_request")
        request_total = round((end - base) * 1000, 1)
        self.last_metrics = {
            "response_id": response_id,
            "stt_total_ms": stt_total,
            "stt_latency_ms": stt_total,
            "vad_finalize_ms": delta("t_vad_end", "t_user_speech_end"),
            "stt_start_delay_ms": delta("t_stt_start", "t_vad_end"),
            "ollama_first_token_ms": first_token,
            "llm_first_token_ms": first_token,
            "ollama_generation_ms": delta("t_ollama_complete", "t_llm_first_token"),
            "ollama_total_ms": delta("t_ollama_complete", "t_ollama_request"),
            "first_sentence_ms": delta("t_first_sentence"),
            "tts_first_audio_ms": delta("t_first_audio", "t_tts_start"),
            "end_to_first_audio_ms": delta("t_first_audio"),
            "playback_start_ms": delta("t_playback_start"),
            "speech_to_playback_ms": delta("t_playback_start", "t_user_speech_end"),
            "request_total_ms": request_total,
            "response_complete_ms": request_total,
            **turn.values,
        }
        return self.last_metrics

    def record(self, event: str, **safe: Any) -> None:
        self._timeline.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{key: value for key, value in safe.items() if key not in {"text", "content", "transcript"}},
        })

    def snapshot(self) -> dict[str, Any]:
        return {"last_metrics": self.last_metrics, "timeline": list(self._timeline)[-60:]}
