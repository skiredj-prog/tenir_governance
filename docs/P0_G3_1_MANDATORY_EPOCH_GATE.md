# P0 G3.1 — Mandatory SQLite epoch-bound admission

**Decision: Experimental mono-host gate PASS (subject to pinned CI). Production G3 = HOLD.**
**Change set:** draft PR #72 stacked on draft #71 → #70 → #69. No merge, cutover, provider execution, release, deployment, REG+ promotion or normative guarantee.

## Threat and previous falsifying counterexample

The original direct `ExperimentalSqliteOperationAuthority.admit_created()` and `SQLiteConsensusBackend.reserve()` could create a second `RESERVED` entry for an operation already claimed by the JSON `DurableOperationRegistry`, bypassing `G3ExperimentalExclusiveOwner.candidate_transition()`. A caller-supplied `g3_checked=True` flag would not fix this; authority must be decided at the actual write boundary.

## Implemented enforcement

1. The core `ExperimentalSqliteOperationAuthority.admit_created()` **always** obtains the cooperating old JSON writer file lock, then opens the SQLite `BEGIN IMMEDIATE` transaction. It rechecks the `experimental_g3_exclusive_epoch` row with `phase=ACTIVE_SQLITE_HOLD`, the immutable `source_path`, the fsynced sentinel, the marker `epoch/source_sha256`, and the current *strictly parsed* JSON registry's frozen SHA-256 and exact native `OperationRecord`. There is no caller-supplied clearance flag. Missing/mismatched epoch, absent marker, source drift, non-canonical JSON or old JSON transition => HOLD.
2. In the **same SQLite transaction**, it inserts an immutable `experimental_native_reservation_intents` row binding operation key, reservation ID, original input SHA-256 and the currently read authority epoch. Only then does the SQLite reservation trigger permit a reservation. The final immutable native `OperationRecord` stores the same `authority_epoch`; exact replay/inspection verifies epoch and intent/reservation links. All writes are atomic with the I8 counter update. The earlier core bug around SQLite foreign-key ordering is addressed by inserting the reserved row before the FK-referencing native operation snapshot.
3. A database-level `BEFORE INSERT` trigger rejects new reservations lacking a matching active-epoch intent, including direct `SQLiteConsensusBackend.reserve()`. Until a future separately qualified G4, `UPDATE` and `DELETE` of reservations are refused, stopping direct commit/release as well. These triggers apply only to an explicitly provisioned *disposable experimental SQLite store*. Normal published production stores are unchanged.

## Adversarial acceptance

- Original negative control inverted: JSON accepts first; direct SQLite admission and raw budget reserve must both reject. No second reservation may appear.
- Concurrent processes, four old-wrapper races plus independent direct SQLite entrypoint races, with and without G3 activation. Exactly one of JSON and SQLite claims the logical operation under participating mono-host protocol.
- Stale already-open JSON instance after sentinel installation, PREPARED versus ACTIVE transition, crash before and after fence, and restart.
- Tampered source, changed epoch/marker, missing marker, wrong operation binding: HOLD and no second effect.
- Out-of-band budget commit/release/delete: HOLD; no external provider invocation or T8.

## Explicitly **outside** the security guarantee

- **Older JSON binaries lacking sentinel checks**: not revoked by file-lock discipline; deployment inventory and mandatory upgrade/fencing are unresolved.
- **Privileged SQL/DDL or raw filesystem modification**: a process that drops the triggers, overwrites both sentinel files, or writes directly to the SQLite DB can still violate the laboratory boundary.
- **Multi-host/distributed writers**: POSIX file locking and this SQLite local database do not provide cross-host consensus or fencing. A Raft-specific authoritative epoch/owner protocol and adversarial partition tests are still mandatory if distributed deployment is intended.
- **Bootstrap window** before the G3 database/triggers are provisioned: only a fully drained and isolated experimental store may enter the protocol. Production inventory and ingress quarantine must prevent rogue writes throughout initialization.
- **Provider effect truth** and actual live `DualKernelGovernor` dispatch: no such authority is exercised. All receipts remain `HOLD` and `execution_authorized=false`.

**Formal classification:** `SCOPED_G3_1_EPOCH_BOUND_ADMISSION_PASS` only after CI validation. `G3_PRODUCTION_CUTOVER=HOLD`; `REG_NORMATIVE_CONFORMANCE=NOT_DEMONSTRATED`. Closing issue #67 requires a separate end-to-end production-path owner proof and explicit approval; PRs remain Draft.
