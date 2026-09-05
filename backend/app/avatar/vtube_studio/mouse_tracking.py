"""Natural, bounded cursor tracking for VTube Studio input parameters."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, hypot
from time import monotonic
from typing import Callable

from app.avatar.vtube_studio.models import MouseTrackingMode


@dataclass(frozen=True, slots=True)
class MouseTrackingFrame:
    mode: MouseTrackingMode
    eye_x: float
    eye_y: float
    head_x: float
    head_y: float
    reset_head: bool = False
    reset_all: bool = False


class MouseTrackingController:
    """Apply deadzone, independent smoothing and velocity limits."""

    DEADZONE = .055
    EYE_TIME_CONSTANT = .07
    HEAD_TIME_CONSTANT = .28
    EYE_MAX_VELOCITY = 7.5
    HEAD_MAX_VELOCITY = 2.1

    def __init__(self, mode: MouseTrackingMode = MouseTrackingMode.HEAD_EYES, *, clock: Callable[[], float] = monotonic) -> None:
        self.mode = MouseTrackingMode(mode)
        self.clock = clock
        self.eye_x = self.eye_y = 0.0
        self.head_x = self.head_y = 0.0
        self._last_at: float | None = None
        self._reset_head = False
        self._reset_all = self.mode == MouseTrackingMode.OFF

    def configure(self, mode: MouseTrackingMode | str) -> None:
        selected = MouseTrackingMode(mode)
        if selected == self.mode:
            return
        previous = self.mode
        self.mode = selected
        self._last_at = None
        self._reset_all = selected == MouseTrackingMode.OFF
        self._reset_head = selected == MouseTrackingMode.EYES and previous == MouseTrackingMode.HEAD_EYES

    def reset_transport(self) -> None:
        self._last_at = None
        self._reset_all = self.mode == MouseTrackingMode.OFF
        self._reset_head = self.mode == MouseTrackingMode.EYES

    def update(self, x: float, y: float, *, speaking: bool = False) -> MouseTrackingFrame:
        now = self.clock()
        elapsed = 1 / 30 if self._last_at is None else min(.1, max(.005, now - self._last_at))
        self._last_at = now
        target_x, target_y = self._deadzone(x, y)
        if self.mode == MouseTrackingMode.OFF:
            target_x = target_y = 0.0
            self.eye_x = self.eye_y = 0.0
            self.head_x = self.head_y = 0.0

        eye_target_x = target_x if self.mode != MouseTrackingMode.OFF else 0.0
        eye_target_y = target_y if self.mode != MouseTrackingMode.OFF else 0.0
        head_influence = .78 if speaking else 1.0
        head_target_x = target_x * head_influence if self.mode == MouseTrackingMode.HEAD_EYES else 0.0
        head_target_y = target_y * head_influence if self.mode == MouseTrackingMode.HEAD_EYES else 0.0

        if self.mode != MouseTrackingMode.OFF:
            self.eye_x = self._approach(self.eye_x, eye_target_x, self.EYE_TIME_CONSTANT, self.EYE_MAX_VELOCITY, elapsed)
            self.eye_y = self._approach(self.eye_y, eye_target_y, self.EYE_TIME_CONSTANT, self.EYE_MAX_VELOCITY, elapsed)
            self.head_x = self._approach(self.head_x, head_target_x, self.HEAD_TIME_CONSTANT, self.HEAD_MAX_VELOCITY, elapsed)
            self.head_y = self._approach(self.head_y, head_target_y, self.HEAD_TIME_CONSTANT, self.HEAD_MAX_VELOCITY, elapsed)

        frame = MouseTrackingFrame(
            mode=self.mode,
            eye_x=self.eye_x,
            eye_y=self.eye_y,
            head_x=self.head_x,
            head_y=self.head_y,
            reset_head=self._reset_head,
            reset_all=self._reset_all,
        )
        self._reset_head = False
        self._reset_all = False
        return frame

    @classmethod
    def _deadzone(cls, x: float, y: float) -> tuple[float, float]:
        bounded_x = min(1.0, max(-1.0, float(x)))
        bounded_y = min(1.0, max(-1.0, float(y)))
        magnitude = hypot(bounded_x, bounded_y)
        if magnitude <= cls.DEADZONE:
            return 0.0, 0.0
        scaled = min(1.0, (magnitude - cls.DEADZONE) / (1.0 - cls.DEADZONE))
        return bounded_x / magnitude * scaled, bounded_y / magnitude * scaled

    @staticmethod
    def _approach(current: float, target: float, time_constant: float, max_velocity: float, elapsed: float) -> float:
        desired = current + (target - current) * (1.0 - exp(-elapsed / time_constant))
        limit = max_velocity * elapsed
        delta = min(limit, max(-limit, desired - current))
        value = current + delta
        return 0.0 if abs(value) < 1e-5 else min(1.0, max(-1.0, value))
