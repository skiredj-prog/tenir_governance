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
