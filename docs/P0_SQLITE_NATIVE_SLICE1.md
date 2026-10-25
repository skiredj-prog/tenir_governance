# P0 #67 — SQLite native operation authority: slice 1

**Status:** isolated integration candidate, **HOLD**, no production deployment.
**Source branch:** `integration/p0-sqlite-authority-slice1` from `main`, deliberately independent from experimental PR #65.

## Decision

For the single-host lane, use the existing `SQLiteConsensusBackend` WAL
transaction as the proposed authority boundary. Unlike the earlier experimental
candidate model, this slice serializes a real `OperationRecord` from TENIR-Gov
and its corresponding reservation in the existing budget tables **atomically**.

The slice is named `ExperimentalSqliteOperationAuthority` to preserve honest
scope. It is **not** a drop-in replacement for `DurableOperationRegistry`;
nothing in the active `DualKernelGovernor` imports or uses it.

## Narrow implemented contract

- Input: one real `OperationRecord` in `CREATED`, canonical payload hash,
  required passage/principal/action/resource, positive bounded amount,
  no pre-existing reservation, no dispatch/commit claim.
- The caller must use a disposable `lab:` operation and resource. Constructor
  requires `experimental_only=True`; these guards prevent accidental use but
  **do not** create an institutional security boundary.
- Under a single `SQLiteConsensusBackend._transaction` (`BEGIN IMMEDIATE`),
  insert one immutable native OperationRecord snapshot with state `RESERVED`,
  an existing-schema reservation and the corresponding I8 `resources.reserved`
  counter update. The operation and reservation share the idempotency key.
- Exact replay rechecks snapshot hash, source identity, reservation state,
  payload, amount and resource I8 ledger totals, and returns the original
  receipt. Divergence, orphan reservation, missing resource, pending
  compensation, UNKNOWN effect, shared non-lab budget or a G3 fence => HOLD.
- Unconditional `decision=HOLD`, `execution_authorized=false`,
  `is_production_authority=false`; **no dispatch, commit, refund, external
  effect or T8 retry** method exists.

## Acceptance tests

The dedicated workflow `P0 SQLite native authority slice 1` executes:
- New tests for atomic registration/reservation, idempotency, replay binding,
  I8 capacity and accounting, crash rollback, cross-process contention,
  immutability, accidental activation, shared budget denial and G3 fencing.
- Existing `tests/test_sqlite_governor_crash_recovery.py` regression tests,
  to ensure the incumbent JSON-registry governor behavior is unchanged.

**No full native T6/T7 conformance claim is permitted.** The published
TENIR-Gov 5.7.0 remains unchanged; REG #47 and #48 are separate evidence lanes.

## Next reviewed integration slices

1. **Slice 2 (design review first):** bound `AttemptState` and a qualified
   signed evidence event to the native operation snapshot and budget
   reservation in the same SQLite database/transaction, preserving traceability.
2. **Slice 3:** explicit durable owner epoch and controlled migration from the
   JSON registry, fencing all registered writers. The cooperative single-host
   G3 experiment from PR #65 is **not** sufficient for production cutover.
3. **Slice 4:** grant consumption and actual governor dispatch with a provider
   effect adapter, never retrying an uncertain external effect blindly.
4. **Slice 5:** REG T6/T7 native qualification on the real integrated governor,
   cross-process, crash/partition (if claimed), budget I1/I8 proof, then T8.

Merge requires independent review and green CI. A successful narrow test
does not authorize production migration, release or normative claims.
