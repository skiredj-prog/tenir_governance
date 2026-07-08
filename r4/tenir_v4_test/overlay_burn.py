from __future__ import annotations

from pathlib import Path

from .ledger import HashChainedLedger
from .models import BurnEstimate, BurnInputs


def estimate_burn_cost(
    ledger_path: str | Path,
    *,
    inputs: BurnInputs | None = None,
) -> BurnEstimate:
    """Optional commercial overlay.

    Salvaged from Writer only as a detached analytic surface.
    It reads the local ledger after the fact and never affects runtime governance.

    Counting rule:
    - intended blocks represent governance interventions
    - alerts represent review burden
    A single observation may contribute to both counts.
    """
    inputs = inputs or BurnInputs()
    inputs.validate()

    ledger = HashChainedLedger(ledger_path)
    intended_block_count = 0
    alert_count = 0

    for entry in ledger.iter_entries():
        payload = entry.get("payload", {})
        if payload.get("type") != "observation":
            continue
        action = payload.get("action")
        trajectory = payload.get("trajectory", {})
        if action in {"allow_with_intended_block", "block"}:
            intended_block_count += 1
        if trajectory.get("alert"):
            alert_count += 1

    return BurnEstimate(
        intended_block_count=intended_block_count,
        alert_count=alert_count,
        cost_inputs=inputs,
    )
