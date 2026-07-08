from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import List


@dataclass
class TrajectorySnapshot:
    s_score: float
    ds_de: float
    d2s_de2: float
    horizon_events: int | None


class TrajectoryKernel:
    """Small event-indexed trajectory kernel for the intake workspace.

    This is intentionally minimal. It preserves the contract expected by
    `r5_server.py` without pretending to replace the main TENIR kernel lines.
    """

    def __init__(self, epsilon: float = 0.01) -> None:
        self.epsilon = epsilon
        self._history: List[float] = []
        self._ds_history: List[float] = []

    def compute(self, pressure: float, velocity: float, capacity: float) -> TrajectorySnapshot:
        denominator = (pressure * velocity) + self.epsilon
        s_score = capacity / denominator
        self._history.append(s_score)

        ds_de = 0.0
        if len(self._history) >= 2:
            ds_de = self._history[-1] - self._history[-2]
            self._ds_history.append(ds_de)

        d2s_de2 = 0.0
        if len(self._ds_history) >= 2:
            d2s_de2 = self._ds_history[-1] - self._ds_history[-2]

        horizon_events = None
        if ds_de < 0:
            horizon_events = max(0, ceil(s_score / abs(ds_de)))

        return TrajectorySnapshot(
            s_score=round(s_score, 6),
            ds_de=round(ds_de, 6),
            d2s_de2=round(d2s_de2, 6),
            horizon_events=horizon_events,
        )
