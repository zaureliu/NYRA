from __future__ import annotations

from collections import deque
from statistics import fmean


class RollingNetworkMetrics:
    def __init__(self, max_samples: int = 900) -> None:
        self.latencies: deque[float] = deque(maxlen=max_samples)
        self.outcomes: deque[bool] = deque(maxlen=max_samples)

    def add_probe(self, reachable: bool, latency_ms: float | None) -> None:
        self.outcomes.append(reachable)
        if reachable and latency_ms is not None:
            self.latencies.append(float(latency_ms))

    def summary(self, window: int = 30) -> dict[str, float | None]:
        latencies = list(self.latencies)[-window:]
        outcomes = list(self.outcomes)[-window:]
        jitter = 0.0
        if len(latencies) > 1:
            jitter = fmean(abs(current - previous) for previous, current in zip(latencies, latencies[1:]))
        loss = (1 - sum(outcomes) / len(outcomes)) * 100 if outcomes else 0.0
        return {
            "average_ms": round(fmean(latencies), 2) if latencies else None,
            "min_ms": round(min(latencies), 2) if latencies else None,
            "max_ms": round(max(latencies), 2) if latencies else None,
            "jitter_ms": round(jitter, 2),
            "packet_loss_percent": round(loss, 2),
        }
