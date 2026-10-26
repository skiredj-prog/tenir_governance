# CB05_CHECKPOINT_SPEC.md — Independent integrity checkpoint
Version 0.1 | 2026-10-25 | **DESIGN ONLY — NOT IMPLEMENTED / NOT VERIFIED**

## Purpose
Make local SQLite rollback or silent history rewriting **detectable relative to an authenticated, independently retained checkpoint**. A verifier must not trust local receipts as the sole authority. This is a detection guarantee under stated trust assumptions, **not** prevention of privileged writes, proof of external effects, or a guarantee that every unanchored mutation is observable.

## Architecture
1. **Merkle Root Publisher (untrusted local producer)** — at a configured epoch/transaction boundary, canonicalizes and hashes the bounded ordered set of committed evidence receipts; constructs a domain-separated Merkle root and a checkpoint envelope. A root alone does not attest that receipts are truthful or complete.
2. **External Anchoring Witness (independent trust domain)** — accepts signed, scoped, monotonic checkpoint submissions; maintains an independently durable append-only head per (realm, store, trust generation); rejects forks or decreasing sequence; returns a signed acknowledgment including sequence, root, predecessor, and witness identity. A copy in the same local writable store is not independent.
3. **Independent Verifier CLI** — consumes a snapshot of local evidence, inclusion/consistency proofs, pinned witness public key and a **trusted latest witness head** obtained independently or from a fresh signed witness statement. Recomputes canonical roots and validates witness signatures, chain continuity, epoch, scope, trust generation and freshness.

## Envelope — proposed fields
`schema_version`, `realm_id`, `store_id`, `trust_generation`, `epoch`, `sequence`, `previous_checkpoint_digest`, `merkle_root`, `leaf_count`, `state_digest`, `quarantine_generation`, `quarantine_state_digest`, `commit_boundary`, `publisher_key_id`, `publisher_signature`. Witness acknowledgment additionally binds `witness_id`, `witness_sequence`, `checkpoint_digest`, `witness_timestamp`, `witness_signature`.

Specify a deterministic canonical serialization, duplicate-key rejection, domain separation for leaves/nodes, hash algorithm, signature suite, key rotation and revocation before implementation. RFC 8785, SHA-256 and Ed25519 are **candidates**, not implemented guarantees.

## Protocol and durability boundary
- Local SQLite commit and remote witness acknowledgment are **not atomic**. Track `LOCAL_COMMITTED_UNANCHORED`, `ANCHOR_PENDING`, `ANCHORED`, `CONFLICT` and `UNKNOWN` with explicit crash recovery.
- An operation that requires anchored assurance MUST NOT be represented as externally finalized while its checkpoint is unanchored; enforce HOLD or an explicitly documented weaker mode.
- Witness must durably persist its monotonic head before acknowledging. Retries are idempotent on exact checkpoint digest and scoped sequence.
- A quarantined state and its transition generation must be covered by the independently anchored state. Merely hashing the SQLite event log does not detect Q6 (quarantine sidecar rollback).
- An authenticated checkpoint does not establish that a provider performed a payment or other external effect; that requires an independently authenticated provider receipt and separate verification.

## Offline verification — qualified
A verifier can check a historical snapshot against a **previously trusted signed anchor** offline without the SUT or execution private keys. Offline verification **cannot guarantee that this anchor is the latest head** without a fresh trusted head, an externally supplied freshness bound, or an independently trusted monotonic record. Report `HISTORICAL_CONSISTENCY_ONLY` rather than `CURRENT_STATE_VERIFIED` when freshness is unavailable.

## Negative vectors / oracles
- N01: coherent SQLite + local checkpoint rollback vs current independent witness head → ROLLBACK/HOLD.
- N02: Q6 restore inactive quarantine JSON + sentinel while SQLite stays current → generation mismatch/HOLD.
- N03: Q1 remove quarantine JSON and sentinel after initialized store → missing history/HOLD.
- N04: same scoped sequence, two distinct roots → FORK/HOLD.
- N05: wrong realm, store, epoch, trust generation, signature or revoked key → REJECT.
- N06: witness unreachable/stale → UNKNOWN/HOLD; never silently trust local-only evidence.
- N07: crash after local COMMIT before witness ACK, and after ACK before caller response → idempotent recovery with explicit pending state.
- N08: valid historical witness proof but no fresh head → historical-only result, never current guarantee.
- N09: privileged local writer alters all local artifacts but not witness → mismatch/HOLD.
- N10: witness compromise/collusion → outside assurance boundary; explicit residual risk.

## Acceptance
1. Third party recomputes Merkle root and verifies inclusion/consistency proof and witness signature with pinned public keys.
2. Fresh independently obtained witness head detects any older **anchored** history substituted locally, including coherent snapshots.
3. Q1/Q6 tested with actual artifacts and recovery paths; fail closed for ambiguity.
4. Fork and sequence replay are rejected by witness and verifier.
5. Real crash injection validates commit/ack windows; no false claims of atomicity.
6. Raw test output, actual artifact SHA-256 manifest, exact SUT SHA, environment and security verdicts are retained.
7. Independent adversarial review by Agent C.

## Explicit limitations
- Transactions since the last anchored checkpoint may be rolled back without detection unless another independent record exists. Bound and declare the exposure window.
- No mathematical detection guarantee if both local store and independent witness/trust roots are compromised.
- Merkle inclusion proves inclusion in the committed set, not completeness of real-world actions.
- Production remains HOLD; this specification is not a deployed checkpoint.
