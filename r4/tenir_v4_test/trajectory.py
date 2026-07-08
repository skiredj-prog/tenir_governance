from __future__ import annotations

from collections import deque
from math import ceil
from typing import Deque, Optional

from .models import EventSample, PolicyBundle, TrajectoryState


def compute_stability(event: EventSample, epsilon: float) -> float:
    """Structural stability proxy.

    S = K / (P * V + epsilon)
    """
    denominator = (event.pressure * event.velocity) + epsilon
    return event.capacity / denominator


class TrajectoryTracker:
    """Event-indexed trajectory tracking.

    This intentionally tracks changes per event rather than wall-clock time.
    """

    def __init__(self, policy: PolicyBundle) -> None:
        policy.validate()
        self.policy = policy
        self._stability_window: Deque[float] = deque(maxlen=policy.event_window)
        self._first_derivative_window: Deque[float] = deque(maxlen=policy.event_window)

    def update(self, event: EventSample) -> TrajectoryState:
        event.validate()
        s = compute_stability(event, self.policy.epsilon)
        self._stability_window.append(s)

        ds_de = 0.0
        if len(self._stability_window) >= 2:
            ds_de = self._stability_window[-1] - self._stability_window[-2]
            self._first_derivative_window.append(ds_de)

        d2s_de2 = 0.0
        if len(self._first_derivative_window) >= 2:
            d2s_de2 = self._first_derivative_window[-1] - self._first_derivative_window[-2]

        projected_events_to_zero: Optional[int] = None
        if ds_de < 0:
            projected_events_to_zero = max(0, ceil(s / abs(ds_de)))

        option_space_low = False
        option_space_block = False
        if event.option_space is not None:
            option_space_low = event.option_space <= self.policy.option_space_alert_floor
            option_space_block = event.option_space <= self.policy.option_space_block_floor

        alert_reasons = []
        if s <= self.policy.s_alert_floor:
            alert_reasons.append("stability floor crossed")
        if ds_de <= self.policy.ds_de_alert_floor:
            alert_reasons.append("stability dropping too fast")
        if d2s_de2 <= self.policy.d2s_de2_alert_floor:
            alert_reasons.append("stability decline is accelerating")
        if (
            projected_events_to_zero is not None
            and projected_events_to_zero <= self.policy.reaction_budget_events
        ):
            alert_reasons.append("human reaction budget nearly exhausted")
        if option_space_low:
            alert_reasons.append("option space materially compressed")

        alert = bool(alert_reasons)

        intended_block_reasons = []
        if s <= self.policy.s_block_floor:
            intended_block_reasons.append("block floor crossed")
        if (
            projected_events_to_zero is not None
            and projected_events_to_zero <= max(1, self.policy.reaction_budget_events // 2)
        ):
            intended_block_reasons.append("collapse horizon critically near")
        if option_space_block:
            intended_block_reasons.append("option space critically compressed")

        intended_block = bool(intended_block_reasons)
        rationale_parts = alert_reasons + [
            reason for reason in intended_block_reasons if reason not in alert_reasons
        ]
        if not rationale_parts:
            rationale_parts = ["within configured envelope"]

        return TrajectoryState(
            stability=round(s, 6),
            ds_de=round(ds_de, 6),
            d2s_de2=round(d2s_de2, 6),
            projected_events_to_zero=projected_events_to_zero,
            option_space_low=option_space_low,
            alert=alert,
            intended_block=intended_block,
            rationale="; ".join(rationale_parts),
        )
