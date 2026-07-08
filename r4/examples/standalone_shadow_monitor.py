"""Self-contained snippet salvaging the portability value from Writer.

This file is intentionally single-file and dependency-light. It is for demos,
sandbox drills, and fast partner workshops. It is not the full package.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Optional


@dataclass
class Event:
    pressure: float
    velocity: float
    capacity: float


def stability(event: Event, epsilon: float = 1e-6) -> float:
    return event.capacity / ((event.pressure * event.velocity) + epsilon)


def evaluate(prev_s: float, event: Event) -> tuple[float, float, Optional[int], str]:
    s = stability(event)
    ds_de = s - prev_s
    projected_events_to_zero = ceil(s / abs(ds_de)) if ds_de < 0 else None

    reasons = []
    if s <= 0.90:
        reasons.append("alert: stability floor crossed")
    if ds_de <= -0.05:
        reasons.append("alert: stability dropping too fast")
    if projected_events_to_zero is not None and projected_events_to_zero <= 5:
        reasons.append("alert: reaction budget nearly exhausted")
    return s, ds_de, projected_events_to_zero, "; ".join(reasons) or "within envelope"


if __name__ == "__main__":
    previous = stability(Event(0.7, 0.6, 1.1))
    for raw in [
        Event(0.8, 0.9, 1.0),
        Event(1.0, 1.1, 0.95),
        Event(1.2, 1.5, 0.70),
    ]:
        s, ds_de, horizon, rationale = evaluate(previous, raw)
        print(round(s, 6), round(ds_de, 6), horizon, rationale)
        previous = s
