"""
5-minute developer gate — synthetic lost-response recovery.

Operationalizes:
- knows when not to act
- evidence-bound recovery
- single authoritative dispatcher and budget owner

Clock starts after preflight. Local, no credentials.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

class Outcome(Enum):
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"

@dataclass
class Effect:
    effect_id: str
    payload: dict
    dispatched: bool = False
    outcome: Outcome = Outcome.UNKNOWN
    receipt: Optional[str] = None

class SingleDispatcher:
    """One authoritative dispatcher and budget owner per mission."""
    def __init__(self):
        self._effects: dict[str, Effect] = {}
        self._budget_reserved = False

    def dispatch(self, effect_id: str, payload: dict) -> Effect:
        if effect_id in self._effects and self._effects[effect_id].dispatched:
            raise RuntimeError("Duplicate dispatch blocked — single dispatcher")
        effect = Effect(effect_id=effect_id, payload=payload, dispatched=True)
        self._effects[effect_id] = effect
        self._budget_reserved = True
        return effect

    def mark_unknown(self, effect_id: str):
        if effect_id not in self._effects:
            raise KeyError(effect_id)
        self._effects[effect_id].outcome = Outcome.UNKNOWN

    def correlate_receipt(self, effect_id: str, receipt: str):
        if effect_id not in self._effects:
            raise KeyError(effect_id)
        effect = self._effects[effect_id]
        if effect.outcome != Outcome.UNKNOWN:
            raise RuntimeError("Receipt only correlates an UNKNOWN outcome")
        effect.receipt = receipt
        effect.outcome = Outcome.CONFIRMED

    def export(self, effect_id: str) -> dict:
        effect = self._effects[effect_id]
        return {
            "effect_id": effect.effect_id,
            "dispatched": effect.dispatched,
            "outcome": effect.outcome.value,
            "receipt": effect.receipt,
            "adapter_version": "5min-gate-scaffold-v0",
        }

def run_synthetic_scenario() -> dict:
    """
    0–1 min: authorized synthetic booking, response deliberately lost
    1–2 min: UNKNOWN
    2–3 min: retry blocked
    3–4 min: correlated receipt
    4–5 min: inspectable export
    """
    d = SingleDispatcher()
    effect_id = "booking-001"

    # 0–1 min
    d.dispatch(effect_id, {"hotel": "synthetic", "guest": "dev"})
    d.mark_unknown(effect_id)  # lost response

    # 2–3 min — retry must be blocked
    try:
        d.dispatch(effect_id, {"hotel": "synthetic", "guest": "dev"})
        raise AssertionError("Duplicate was not blocked")
    except RuntimeError:
        pass  # expected

    # 3–4 min
    d.correlate_receipt(effect_id, "receipt-corr-001")

    # 4–5 min
    return d.export(effect_id)

if __name__ == "__main__":
    result = run_synthetic_scenario()
    print(result)
    assert result["outcome"] == "CONFIRMED"
    assert result["receipt"] == "receipt-corr-001"
    print("5-minute gate scaffold: PASS")
