# P0 blocking gate — G3 concurrent JSON/SQLite ownership

**Decision:** no production cutover, no merge, no REG/REG+ promotion. Draft engineering experiment layered on Slice 2.

## Adversarial invariant

For a single logical operation `lab:op-1`, arrange a simultaneous transition:

- Existing, persistent `DurableOperationRegistry` JSON tries `CREATED → RESERVED`.
- New, experimental SQLite native authority tries to reserve **the same logical key** using `ExperimentalSqliteOperationAuthority`.

**Must never observe two successful authorizations**. A successful JSON transition makes SQLite ownership activation fail closed. A completed SQLite fencing ceremony blocks the JSON transition even when the registry instance was constructed before the marker existed. Recovery of an unfinished ceremony is HOLD until the owner epoch, source snapshot and durable files agree.

## Cooperative lab protocol

1. Under the existing cross-process JSON writer file lock, inspect the real JSON source, requiring CREATED only and no in-flight/settled operations.
2. Persist SQLite `PREPARED` epoch with source fingerprint. At this stage SQLite cannot yet admit.
3. Fsync `.g3-sqlite-owner.initialized`, then fsync the owner marker. The updated JSON registry checks either file **under its own writer lock** before *every* write, including old objects already constructed.
4. Commit SQLite epoch `ACTIVE_SQLITE_HOLD`. The G3-scoped SQLite entry point rechecks the same legacy source, epoch and both marker files while holding the JSON writer lock; it may stage a *lab-only* native RESERVED row and reservation, still with `decision=HOLD` and `execution_authorized=false`.

A crash after `PREPARED` but before the sentinel may leave JSON authoritative; SQLite remains HOLD. A crash after the sentinel makes JSON HOLD and SQLite remains HOLD until the ceremony is explicitly recovered. Missing finalized markers are not automatically recreated. The fence and epoch are monotonic and non-reversible by normal operations.

## Scope and nonclaims

This is **cooperative single-host** fencing, not deployment-wide cryptographic revocation. **Unpatched pre-fence JSON binaries can bypass the new check; direct SQLite APIs not routed through the G3 gate can bypass its wrapper; privileged file deletion or remote-host writers remain uncontrolled.** Before *any production cutover*, the system must enumerate and fence every deployed writer and make this restriction unavoidable across all supported entry points. Until then, G3 is **P0 HOLD**, even when the concurrent laboratory test passes.

No actual external execution, dispatch grant, budget settlement or T8 retry is permitted. REG and public guarantee promotion stay blocked.

## Acceptance evidence

The workflow `P0 SQLite native authority and G3 double owner` runs the real two-process race multiple times, both deterministic owner orderings, pre/post-sentinel kill-9 analogues (`os._exit`), restart, missing-marker fail-close, and the full native operation/attempt + incumbent crash-recovery regressions. Publication of the result must refer to an exact successful CI run; a green candidate is not a production G3 sign-off.

## Negative control — why production G3 is STILL BLOCKED

A separate regression deliberately invokes the **unrouted** `ExperimentalSqliteOperationAuthority.admit_created()` after the old JSON registry has already accepted `CREATED → RESERVED`. Today **both lab stores can retain the same logical operation** if callers bypass `G3ExperimentalExclusiveOwner`. This is a controlled reproduction of the defect class, **not a passing conformance invariant**.

The cooperative G3 wrapper prevents dual authorization only for participating code paths. Production closure requires **eliminating or hard-blocking every alternative SQLite entry point**, upgrading/fencing every legacy writer process and proving no direct backend writes are possible. The future production acceptance test must reject this negative control, not simply exclude it from CI. Until then: **G3 production = HOLD / cutover forbidden**.
