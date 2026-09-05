from .models import CanonicalTranscript


class TranscriptAssembly:
    """Keep finalized time ranges once; interims never enter the final buffer."""

    def __init__(self):
        self.segments: dict[tuple[float, float], CanonicalTranscript] = {}
        self.duplicates = 0

    def add(self, transcript: CanonicalTranscript) -> bool:
        if not transcript.is_final or not transcript.text.strip():
            return False
        key = (round(transcript.started_at, 3), round(transcript.ended_at, 3))
        if key in self.segments:
            self.duplicates += 1
            return False
        # Retransmission can cover a subset of an already finalized interval.
        if any(start <= key[0] and end >= key[1] for start, end in self.segments):
            self.duplicates += 1
            return False
        # Overlapping/cumulative results can include words finalized before.
        # Use word times to retain only new speech, not text-string heuristics
        # that would erase an intentional repetition later in the utterance.
        if transcript.words and self.segments:
            covered = [(word.started_at, word.ended_at) for segment in self.segments.values() for word in segment.words]
            fresh = [word for word in transcript.words if not any(start <= word.started_at + .001 and end >= word.ended_at - .001 for start, end in covered)]
            if len(fresh) != len(transcript.words):
                self.duplicates += 1
                if not fresh:
                    return False
                transcript = transcript.model_copy(update={"text": " ".join(word.text for word in fresh), "words": fresh,
                                                           "started_at": fresh[0].started_at})
                key = (round(transcript.started_at, 3), key[1])
        self.segments[key] = transcript
        return True

    def finish(self, empty: CanonicalTranscript) -> CanonicalTranscript:
        segments = [self.segments[key] for key in sorted(self.segments)]
        if not segments:
            return empty
        confidences = [item.confidence for item in segments if item.confidence is not None]
        return empty.model_copy(update={
            "text": " ".join(item.text.strip() for item in segments),
            "started_at": segments[0].started_at,
            "ended_at": max(item.ended_at for item in segments),
            "speech_final": any(item.speech_final for item in segments),
            "words": [word for item in segments for word in item.words],
            "confidence": sum(confidences) / len(confidences) if confidences else None,
        })
