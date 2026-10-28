# W2.2a-ter — Paths documented for Agent 2 / W3

## Integrated path (current)
- `run_integrated_candidate` uses `NativeAuthorityGate`.
- Grant is evaluated **before** any I8 reservation or provider call.
- On `execution_not_authorized` (historical G4-A): no reservation, no provider call, no settlement.

## Direct / legacy paths (still exist, not closed by this lab)
- `ProofProvider.execute()` can be called directly without a grant.
- Historical G4-A (`experimental_g4a_settlement.py`) remains a separate simulated authority with `execution_authorized=false`.
- These paths are out of scope for the integrated candidate and are documented for B1/W3 adversarial testing.

## B1 status
Not closed. Direct provider API and historical G4-A bypass remain possible outside the integrated function.
