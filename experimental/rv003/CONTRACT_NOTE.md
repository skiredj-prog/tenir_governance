# RV-003 Native Governor × Lab Receiver — Contract note (corrected)

**Date:** 15 November 2026  
**Status:** Adapter skeleton ready; qualification NOT RUN  
**Native pin:** `674c8606089251ea2f74890bd47bce3a717c3b91`  
**Adapter:** `rv003_dispatch_provider.py` (external)

## Mandatory mapping correction

| Receiver state | Native outcome | Notes |
|----------------|----------------|-------|
| ACCEPTED / QUEUED / ACCEPTED_PAUSED | PENDING | Accepted, not finished |
| COMMITTED + durable effect = 1 | CONFIRMED | Only with ledger + consumption proof |
| DENIED_REVOKED + effect = 0 + terminal + no replay | FAILED | Authoritative terminal non-execution only |
| HOLD_AUTHORITY_UNKNOWN / CHANGED | PENDING or UNKNOWN | Never FAILED |
| Timeout / crash / contradictory | UNKNOWN | Never FAILED on mere absence |

## Invariants

1. `represented_user_sub` in metadata is caller-supplied; marked unverified.
2. Reconciliation exercises native `Governor.reconcile_unknown()` path.
3. No automatic re-dispatch inside reconcile.
4. Idempotency conflict (different payload/identity) fails closed.
5. No CONFIRMED without durable receiver effect evidence.
6. Negative tests required:
   - Premature FAILED on HOLD
   - Premature CONFIRMED without effect
   - Reconciliation after receiver restart

## Next wiring steps

- Inject the real `control()` callable from the existing `receiver_p.py`.
- Wire native Governor (frozen checkout) to this provider.
- Run the causal chain: admission → dispatch → ACCEPTED → revocation → DENIED → witness 0 effect → Governor reconciliation.
- Preserve all negative results and declare races R1/R2 explicitly.

Integration Lock / E2E / Production remain HOLD.
