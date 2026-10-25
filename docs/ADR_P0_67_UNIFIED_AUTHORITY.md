# ADR-P0-67 — One authoritative transaction for operation, attempt and settlement

Status: **CANDIDATE ARCHITECTURE / NOT APPROVED FOR PRODUCTION**
Date: 2026-10-25
Tracking: TENIR-Gov #67; source PR #65; REG candidate PR #48.

## Decision boundary

The current governor persists `OperationState` in the JSON-backed `DurableOperationRegistry`. SQLiteConsensusBackend separately owns resource and reservation accounting; the experimental T6/T7 attempt ledger is another independent SQLite authority. No global atomic transaction exists. A lock or two reads cannot close this gap, and an Ed25519 signature does not certify the truth of a provider's claim.

**Candidate direction:** converge authoritative operation state, attempt qualification, evidence, reservation ownership and commit transition within **one transactional backend** for the single-host lane, rather than adding a fourth independent two-phase journal. The existing SQLiteConsensusBackend is the logical schema/transaction host to evaluate first; its transaction boundary is already used for I8 budget invariants. This is a recommendation for design review, not permission to change live governor authority.

## Implemented evidence

- `AtomicAttemptCandidateAuthority`: experimental source projection, attempt and signed evidence event in ONE SQLite WAL transaction; signed/nonce/LEI/operation fingerprint binding.
- Terminal and identity immutability plus append-only event guards.
- 2-process contradictory signed outcomes, hard exits before/after commit, exact replay and restart.
- Same-file coexistence with `SQLiteConsensusBackend`, without secretly invoking `reserve`, `commit`, `release` or `refund`. **Coexistence is not one transaction.**
- [TENIR candidate CI 40168586084](https://github.com/skiredj-prog/tenir_governance/actions/runs/40168586084): 69/69 PASS; [REG independent CI 40168660397](https://github.com/skiredj-prog/reg-conformance/actions/runs/40168660397): PASS with non-promotion assertions and SHA-256 artifact.
- Existing read-only observation API remains unconditional HOLD.

## Migration contract — acceptance gates, not assertions of completion

| Gate | Required behavior | Current status |
| --- | --- | --- |
| G0 inventory | Freeze authoritative JSON registry under shared admission/quarantine lock; export immutable operation IDs, state, payload digest, reservations and evidence hashes; refuse malformed/duplicate/unresolved ownership | NOT IMPLEMENTED |
| G1 atomic schema | One canonical operation/attempt/evidence/reservation ownership transition in one DB transaction, with immutable actor/scope/operation fingerprint and schema guard | CANDIDATE for operation projection + attempt + evidence only; budget separate calls |
| G2 shadow migration | Compare original JSON operation records to new SQLite source records for each key; fail-closed on mutation/missing records, version drift or stale reads; no write authority | NOT IMPLEMENTED |
| G3 authoritative cutover | One-time exclusive owner/epoch/cutover record, former store fenced read-only, idempotent restart; no dual-active registry writers | NOT IMPLEMENTED |
| G4 commit/dispatch | Bound single-use grant consumed under authoritative transaction; provider call only after durable dispatch claim, with UNKNOWN reconciliation and late-effect compensation; preserve I1/I8 | NOT IMPLEMENTED |
| G5 proof | Systematic process exits at every boundary, simultaneous workers, quarantine, lost provider replies, invalid signer, expired/revoked authority, transaction rollback, budget settlement, power-loss scope and replay evidence | PARTIAL laboratory evidence; production gate untested |
| G6 standardization | Native REG CT-R4-011/012 and eventually CT-R4-013 wire vectors, independently replayable full evidence archive and no false equivalence with reference runner | NOT IMPLEMENTED |

## Explicit invariants

1. A qualified claim in the attempt ledger is not itself an authorization to execute or alter `OperationState`.
2. A timeout, lost reply or stale checkpoint must not establish non-execution.
3. A budget reservation cannot be refunded/released solely because the *candidate* attempt is FAILED.
4. Cross-store inconsistent states require HOLD and durable reconciliation, not inference.
5. No new attempt may be admitted on the same LEI until independently governed T8 retry authority exists.
6. Any source mismatch, untrusted signature, missing owner/epoch or active quarantine blocks dispatch; there is no fail-open fallback.
7. Production guarantee claims must cite the exact release commit, environment, backend and test artifact. Green candidate tests do not promote the published 5.7.0.

## Non-goals

This ADR does not migrate any production data, change the governor dispatch path, modify public releases, claim cross-host consensus or authorize PR merging. Production implementation must be separately reviewed for backward-compatible migration and the external provider/budget fault model.

## G0/G2 shadow inventory — implemented and verified (2026-10-25)

The experimental `ShadowOperationMigrator` creates a **frozen SQLite projection** of the actual JSON-backed `DurableOperationRegistry` without altering the source or the governor's execution path.

- G0: captures every source record under the same cross-process JSON writer file lock used by `register` and `transition_to`, while also holding the source quarantine gate and local registry lock. Rejects corrupt/missing/empty source, duplicate JSON keys at any depth, non-finite JSON values, unknown record fields, key/record mismatch, unroundtrippable data, quarantine and oversized inventory.
- G2: validates immutable canonical record hashes and ordered-inventory SHA-256 across the entire source; stages the first inventory in **one SQLite WAL transaction** and permits only exact idempotent rechecks. New/changed/missing source records or damaged shadow rows are a **HOLD**, never an incremental unreviewed update.
- Crash scenarios: an injected exception or child-process hard exit before commit leaves no shadow epoch or partial rows. Two worker processes converge on one identical epoch; registration from another process waits for capture lock, then causes visible source drift after its own successful write.
- source [CI 40169534749](https://github.com/skiredj-prog/tenir_governance/actions/runs/40169534749) **88/88 PASS** at source commit `fe01013f635ba6117bcce56048b36beb6729ebec`. [Governance CI 40169534772](https://github.com/skiredj-prog/tenir_governance/actions/runs/40169534772) PASS.
- Independent REG candidate [CI 40169588262](https://github.com/skiredj-prog/reg-conformance/actions/runs/40169588262) PASS, classifying `shadow_registry_migration.status=SCOPED_SHADOW_CAPTURE_PASS`, `cutover_performed=false` and `execution_authorized=false`.

**Remaining production gates**: G1 canonical operation+attempt+budget authority in one transaction, G3 explicit durable owner epoch/fencing and backwards-compatible migration, G4 actual dispatch/commit integration, G5 crash + provider + I1/I8 testing. G0/G2 must not be interpreted as an authorization to cut over or delete JSON source.

**Known limits:** the frozen snapshot proves identity at capture/check time on one host, not permanently synchronized state, monotonic source version fencing, multi-host linearizability, factual correctness of underlying records, or immunity to source ABA between independent runs. Legacy JSON records that do not round-trip exactly require a reviewed transformation/migration plan, not an automatic acceptance.

## G1 budget-parity preflight — SCOPED evidence, NOT single authority

The candidate `ShadowBudgetParityAudit` checks an already-frozen G0/G2 source epoch against `SQLiteConsensusBackend` reservations/resources **while holding** the JSON source writer lock, quarantine gate and one SQLite `BEGIN IMMEDIATE` transaction. This provides a bounded, single-host capture consistency check, **not** a durable unified governor/attempt/budget operation transition.

- Every observed budget reservation must refer to a source operation whose `reservation_id`, key, resource, amount and budget ownership flags match. Orphan/duplicate reservations, missing links, `UNKNOWN` reservation states, unrecognized compensation statuses, and counter arithmetic violating I8 are rejected. The preflight deliberately refuses ambiguous legacy records rather than synthesizing ownership.
- The frozen G0 source SHA-256 and canonical budget SHA-256 are written to an immutable G1 parity epoch in the same SQLite transaction; later source or reservation drift causes HOLD.
- The preflight never changes the production JSON registry, budget counters, reservation state, provider effect or candidate dispatch authority. Its report explicitly sets `execution_authorized=false`, `cutover_authorized=false`.
- [Source CI 40169877931](https://github.com/skiredj-prog/tenir_governance/actions/runs/40169877931): **97/97 targeted tests PASS**, nine new G1 tests. [Governance CI 40169877940](https://github.com/skiredj-prog/tenir_governance/actions/runs/40169877940) PASS, on `22e0452150e382b87eba012eb6eac7c001737722`.
- [Independent REG candidate CI 40169963549](https://github.com/skiredj-prog/reg-conformance/actions/runs/40169963549): PASS on the same source SHA, with machine classification `SCOPED_G1_PARITY_PASS` and no-authority checks.

**G1 actual transactional authority is not completed.** This is a G1 *preflight* that can block unsafe migration; it does not give the governor a shared atomic `OperationState` + attempt + reservation-commit transition. G3 fencing/cutover, G4 provider dispatch and G5 complete crash/safety proof remain OPEN.

## G3 experimental fenced shadow — tested, no executable authority

The candidate `G3ShadowFenceCoordinator` introduces a **cooperative single-host writer fence** for updated `DurableOperationRegistry` implementations only. This is deliberately **not** a live governor backend transfer.

**Prerequisites**: frozen G0/G2 source, matching G1 source/budget digest, non-corrupt and non-quarantined JSON store, only CREATED/HOLD operations in the G0 snapshot, **no historical reservations at all**, and drained SQLite budget counters. Ambiguous provider-effect, dispatched, compensated or committed operations cannot use the prototype ceremony.

**Durable sequence under the JSON registry writer lock and quarantine guard**:
1. A SQLite `shadow_authority_fence` epoch commits `PREPARED` with a unique fence ID and source/budget hashes. A crash here creates **no new authority**. The old writer remains available until a durable fence file exists, and any source drift on retry is a HOLD.
2. A persistent `.authority-fence.initialized` sentinel is fsynced before writing `.authority-fence.json`; updated legacy writers check either file under their normal writer lock and refuse subsequent JSON mutations. Deleting only the JSON marker cannot reopen writes. A crash after the marker leaves the old writer fenced and the SQLite epoch still PREPARED.
3. The SQLite epoch advances monotonically to `FENCED_SHADOW_HOLD`. It can be replayed/recovered idempotently only when the frozen hashes and marker still agree. An inspector rechecks the current G1 parity. **Every result carries `execution_authorized=false`, `candidate_dispatch_authorized=false` and `is_production_cutover=false`.**

**Evidence**: [TENIR-Gov CI 40171276528](https://github.com/skiredj-prog/tenir_governance/actions/runs/40171276528) **109/109 scoped tests PASS** on `4def8c9fd39ca758ef271be20d04a8916d67d6c1`, including child-process hard exits before/after marker, subsequent restart, two concurrent ceremonies, stale JSON registry instances, forbidden mutations and monotonic epoch SQL constraints. [Governance CI 40171276493](https://github.com/skiredj-prog/tenir_governance/actions/runs/40171276493) PASS. [Independent REG experiment 40171390160](https://github.com/skiredj-prog/reg-conformance/actions/runs/40171390160) PASS, with `SCOPED_G3_FENCED_HOLD_PASS`.

**Critical limitations**:
- Older pre-fence binaries that do not check this new sentinel, privileged users deleting **both** durable fence files, and multi-host writers are not excluded by this demonstration. This is a *cooperative single-host* fence, not cryptographic revocation or distributed fencing.
- The marker may be installed during a crash and leave the old registry permanently HOLD; there is **no automatic unfence** or production rollback path. Institutional recovery must be independently authorized, specified and tested.
- Preexisting provider calls or unobserved effects are not cancelled. The drained precondition is intentionally strict.
- G3 does not make the candidate SQLite projection an active, authenticated governor owner. The production governor still references JSON; no atomic operation/attempt/budget/dispatch transaction has been deployed. G3 **readiness/fencing is tested, G3 production cutover remains HOLD**.

## G3 finalized-marker tamper gate — verified

A finalized `FENCED_SHADOW_HOLD` epoch **must not automatically recreate** a deleted initialized sentinel or JSON fence marker. An attempted replay after such loss raises `ShadowMigrationHold` and preserves the surviving file's writer block. This separates legitimate restart of an unfinished PREPARED ceremony from attempted silent repair of an already finalized ownership boundary.

- [TENIR-Gov source CI 40171619527](https://github.com/skiredj-prog/tenir_governance/actions/runs/40171619527): **110/110 scoped tests PASS**, pinned implementation `31da28b33cffec474c6321f139032e121b61860e`; [Governance CI 40171619587](https://github.com/skiredj-prog/tenir_governance/actions/runs/40171619587): PASS.
- [REG independent candidate 40171689306](https://github.com/skiredj-prog/reg-conformance/actions/runs/40171689306): PASS, asserting G3 missing-finalized-marker HOLD and that the remaining sentinel still blocks an already-open legacy registry; [REG Capabilities 40171689307](https://github.com/skiredj-prog/reg-conformance/actions/runs/40171689307): PASS.
- `FENCED_SHADOW_HOLD` is **not** `COMMIT_AUTHORIZED`. Recovery requires a separately authorized, designed and tested procedure; privileged deletion of all fence files, unpatched legacy clients, multi-host stores and in-flight provider effects remain outside the current guarantees.
