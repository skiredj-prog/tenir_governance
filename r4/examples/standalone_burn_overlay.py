"""Self-contained burn-cost snippet.

Detached by design: this is presentation math, not runtime governance.
"""

from __future__ import annotations


def estimate_burn_cost(
    intended_block_count: int,
    alert_count: int,
    cost_per_intended_block: float = 500.0,
    cost_per_alert: float = 50.0,
    review_minutes_per_alert: float = 12.0,
    cost_per_review_minute: float = 4.0,
) -> float:
    decision_cost = (intended_block_count * cost_per_intended_block) + (alert_count * cost_per_alert)
    review_cost = alert_count * review_minutes_per_alert * cost_per_review_minute
    return round(decision_cost + review_cost, 2)


if __name__ == "__main__":
    total = estimate_burn_cost(intended_block_count=3, alert_count=5)
    print(total)
