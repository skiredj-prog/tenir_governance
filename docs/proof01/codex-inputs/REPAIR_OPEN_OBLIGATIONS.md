# Open proof obligations (26 October 2026)

| ID | Observation / obligation | Status |
|---|---|---|
| B1-G4A-INGRESS | Direct G4-A calls bypass optional witness wrapper; prove every observable effect gated through owner | **OPEN / SECURITY FAIL** |
| B1-SAME-UID | Same-UID direct SQLite mutation bypasses cooperative release guard (`test_c5_direct_sql_bypass_negative_control`) | **OPEN / SECURITY FAIL** |
| Q1 | Quarantine state erasure may restore admission on unfenced paths | **OPEN / SECURITY FAIL** |
| Q6 | Historical rollback may restore admission on unfenced paths | **OPEN / SECURITY FAIL** |
| B2-WITNESS | External monotonic pin, ownership isolation, rollback resistance, atomicity-gap handling | **OPEN** |
| B3-REPAIR | Fixture-only signed effects, bounded local I8 repair and interrupted SQLite tx | **LOCAL LAB EXERCISED** |
| B4-DIFFERENTIAL | Two independently implemented verifiers + independent key/checkpoint pin | **OPEN** |
| REAL-G4A | Live G4-A ledger/provider integration and all effect/authority routes | **NOT IMPLEMENTED** |
| PILOT | Provider credentials, operator-owned deployment manifests, SLA and threat model | **HOLD** |

`Q1/Q6` belong to **Safety / State Integrity**, not Liveness. RFC-5 §4's new authorization on reinsertion is related but separate. No existing frozen RFC changed.
