# RV-003 R1/R2 final qualification — consolidated evidence

Native SHA verified: 674c8606089251ea2f74890bd47bce3a717c3b91
Status: LABORATORY / OFFLINE / MOCK AUTHORITY

## Results
- R1 crash-after-durable-commit: effect remains COMMITTED (1 effect), recoverable. Absence of ACK does not erase it.
- R1 recovery-reconcile: COMMIT_CHECKING preserved after crash and after restart QUERY_STATUS. No redispatch. No forced terminal decision.
- R2 stale visibility: R2_STALE_VISIBILITY_NOT_EXERCISED. No stale ALLOW observed after revocation ACK under 500 ms lag.

## lag_ms semantics
Visibility lag: evaluate() uses asof = monotonic_now - lag_ms. Rules closed after asof remain visible until lag elapses.

## Contents
- r1_after_commit/ : store, receipts, mock log
- r1_reconcile/ : store, receipts, mock log
- r2_stale/ : store, receipts, mock log
- r1_r2_final.py, r1_r2_final_results.json
