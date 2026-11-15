# RV-003 R2 — Architectural gap (synthetic receiver + mock authority)

## Observed
Under visibility lag (asof = now - lag_ms), a commit can succeed after a revocation ACK
because the rule closure is not yet visible to evaluate().

5/5 runs: ACCEPTED_PAUSED → REVOKE ACK (HTTP 204) → commit → COMMITTED + 1 effect + 1 consumption.

## Scope
- Synthetic lab receiver only.
- Mock authority with explicit visibility lag.
- Not demonstrated on live Magertron.
- Not demonstrated on the full native TENIR-Gov Governor chain.

## Implication
Re-checking permission at commit is insufficient if the authority can answer from a stale view.
A revocation fence or monotonic freshness epoch, verifiable by the receiver at commit time,
would be required to make revocation immediately effective against a lagged authority.

## Status
R2_VIOLATION_OBSERVED — preserved, not reworked to green.
Integration Lock / E2E / Production: HOLD.
