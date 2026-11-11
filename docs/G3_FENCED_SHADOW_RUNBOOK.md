# G3 Fenced Shadow Runbook — Experimental / NOT a production cutover

**Status:** Engineering test protocol. No execution rights or merge approval.
**Scope:** single-host, patched JSON-registry writers, one pinned SQLite candidate.
**Owner:** requires an institutional operator and a separate approved cutover design.

## Preconditions for the experimental ceremony

1. Confirm every registry-writing process uses the **same patched source** with `_assert_legacy_write_unfenced()`. If any old/unpatched binary, direct JSON writer, remote process or privileged writer cannot be enumerated and isolated, **HOLD**. The tool cannot prove deployment-wide writer inventory.
2. Stop all incoming execution traffic, provider sends and recovery workers. Verify out-of-band that no provider effect remains in flight; local OperationState is not enough evidence.
3. Preserve immutable backups of JSON, lock/quarantine markers, budget SQLite plus WAL/SHM and artifact hashes. Do not restore any individual file in isolation.
4. Run G0/G2 and G1 against one consistent offline source. Any malformed source, duplicated key, orphan/UNKNOWN reservation, outstanding budget, source drift or quarantine => **HOLD**.
5. G3 candidate accepts only CREATED/HOLD source records, zero reservation history and zero used budget counters. This is deliberately stricter than a general production migration.

## Durable state and recovery matrix

| SQLite epoch | Fence sentinel | JSON marker | Meaning / allowed action |
|---|---|---|---|
| Absent | Absent | Absent | No ceremony; legacy JSON remains the only registry authority |
| PREPARED | Absent | Absent | Crash before fencing; **do not claim transfer**; recheck both baselines before retry |
| PREPARED | Present | Absent | Partial installation; legacy writes blocked; replay only if baselines still match |
| PREPARED | Present | Present and valid | Old writes blocked; finish candidate HOLD epoch after validation |
| FENCED_SHADOW_HOLD | Present | Present and valid | Shadow remains **non-executable**; observer verifies G1 source/budget before reporting |
| FENCED_SHADOW_HOLD | Any file missing or corrupt | Any | **HOLD**; no self-healing or deletion; institutional recovery incident |
| Any | Files removed by privileged actor | Files removed | Outside this prototype's threat model; no secure execution claim |

The sentinel and marker are not cryptographically authenticated and do not fence old binaries which lack the check. Do not advertise this as multi-host leader election, consensus, cryptographic revocation, or proof of exactly-once effects.

## Forbidden transitions

- Never map `FENCED_SHADOW_HOLD` to `NORMAL`, `PASS`, a durable dispatch claim, provider execution, budget settlement or T8 retry.
- Do not rewrite/delete the legacy source, ledger WAL, shadow epoch or fence files.
- Do not bypass a marker mismatch by rerunning G3 with a new epoch. There is **no automatic rollback or unfence**.
- Do not merge PR #65 or propagate REG/REG+ conformity on this experiment.

## Remaining operational readiness gates

- G1: a genuine single authoritative transaction covering live OperationState, attempt, signed evidence and budget settlement.
- G3: authenticated approval/fencing across **all deployed writers**, bounded owner epoch, restart/rollback or recovery ceremony and safe backout plan.
- G4: provider dispatch claiming, late-reply reconciliation and I1/I8 semantics under process failures.
- G5: independent adversarial testing, including cross-host partitions if multi-host is claimed.

Evidence: TENIR-Gov [110 scoped tests PASS](https://github.com/skiredj-prog/tenir_governance/actions/runs/40171619527), REG [isolated G3 probe PASS](https://github.com/skiredj-prog/reg-conformance/actions/runs/40171689306). Both are experimental and not release conformance.
