# CB-05 — Independent checkpoint and authenticated provider evidence
Version: architecture candidate v0.2 | 2026-10-25 | **SPECIFICATION ONLY — NO IMPLEMENTATION OR TEST EVIDENCE**

## Scope and claims
G4-A is POSIX single-host simulated settlement. CB-05 must detect rollback/fork against an independently maintained anchor and fail closed on uncertainty. It cannot retroactively prevent a privileged local write, prove all external effects, or guarantee capture of an uninstrumented writer. Distinguish (A) state integrity attestation and (B) authenticated provider effect receipts.

## Trust domains
- **Runtime/store**: SQLite, G3 source, quarantine JSON/sentinels; potentially compromised by privileged local writer. Not an independent anchor.
- **Checkpoint authority**: separate identity and write permissions, protected signing key, independently durable monotonic head. A second file on the same writable filesystem or a key accessible to the runtime is NOT independent.
- **Verifier**: separately deployable, configured with pinned checkpoint authority identity and trust generation, able to fetch the latest authenticated head directly from the anchor.
- **Provider**: separately authenticated principal with its own keys/transport, producing receipts for actual external effects. Runtime-signed claims about a provider are not provider evidence.

## Canonical checkpoint envelope (candidate schema)
```json
{
  "schema": "tenir.cb05.checkpoint.v1",
  "realm_id": "lab-realm",
  "store_id": "lab-store",
  "trust_generation": 1,
  "epoch": 1,
  "sequence": 42,
  "previous_checkpoint_digest": "sha256:...",
  "state_digest": "sha256:...",
  "event_head_digest": "sha256:...",
  "quarantine_generation": 7,
  "quarantine_state_digest": "sha256:...",
  "commit_boundary": "g4a-transaction-identifier",
  "issued_at": "RFC3339 UTC timestamp",
  "signer_key_id": "checkpoint-authority-key-id",
  "signature": "base64url detached signature over canonical envelope without signature"
}
```
Canonicalization (e.g. RFC 8785) and cryptographic suite (e.g. Ed25519) are **design proposals**, not implemented G4-A properties. Reject duplicate JSON keys, unknown critical fields, non-canonical encodings, invalid numeric ranges and untrusted keys. Bind realm/store/epoch/trust generation and version in the signature.

## Verification and decision rules
1. Obtain **fresh** independently authenticated latest anchor head. Never rely solely on a checkpoint bundled with the potentially rolled-back SQLite snapshot.
2. Verify authority signature and pinned identity, schema, scope, epoch, trust generation, and revocation state.
3. Recompute local SQLite canonical state and event head and the quarantine sidecar/sentinel state, using a documented snapshot boundary.
4. Compare sequence and previous digest to independently stored head. Same sequence with different digest = fork; lower sequence = rollback; missing predecessor = gap. A higher unanchored sequence is not automatically accepted.
5. Reject stale, unreachable, conflicting, or untrusted anchor with HOLD; do not silently fall back to local-only checks.
6. Explicitly define freshness SLA and maximum lag: **unresolved parameters**. A checkpoint acknowledged by an anchor must be durably recorded before its acknowledgment is relied upon.
7. Never treat an anchor checkpoint as proof that an external provider performed an effect.

## Critical atomicity caveat
SQLite COMMIT and remote anchor acknowledgment cannot form one atomic transaction without an additional protocol. Design must specify pending/anchored states, retry/idempotency, crash recovery and which operations remain HOLD while anchor lags. A signature over local post-commit state without an independently observed head does not resolve this window. The quarantine sidecar is a distinct persistence domain; bind its generation and authenticated state to prevent Q1/Q6 rollback.

## Provider evidence envelope (separate)
`provider_id`, `provider_key_id`, `realm_id`, `operation_key`, `request_id`, `idempotency_key`, `effect_id`, `effect_type`, `effect_status`, `provider_timestamp`, `nonce`, `payload_digest`, `signature`. Validate provider-issued signature and scope, replay protection, expiry/revocation, and semantic mapping of status. If the provider has no authenticated receipt protocol, report **NOT DEMONSTRATED**; do not synthesize evidence.

## Negative test vectors
- CB05-N01 restore coherent older SQLite snapshot with its old local checkpoint; external anchor must detect rollback.
- CB05-N02 restore valid inactive quarantine mark/sentinel (Q6-A) while SQLite remains current; must HOLD on generation/head mismatch.
- CB05-N03 delete mark and sentinel after initialization (Q1); must not silently become first-run.
- CB05-N04 present two distinct checkpoint digests at same realm/store/epoch/sequence; detect fork.
- CB05-N05 replay valid signature from another realm/store or trust generation; reject.
- CB05-N06 anchor unreachable/stale/revoked; HOLD with explicit reason.
- CB05-N07 crash after SQLite COMMIT before anchor ACK, and after anchor ACK before local response; ensure idempotent recovery, never claim atomic remote commit.
- CB05-N08 forged/replayed/stale provider receipt; reject; without provider keys mark BLOCKED.
- CB05-N09 privileged local writer rewrites SQLite and all local sidecars but not remote anchor; detect mismatch on fresh verification.
- CB05-N10 compromised anchor key or colluding anchor; document trust-boundary failure, not a security PASS.

## Evidence and acceptance
Each test: exact SUT SHA, environment, independently measured anchor head, before/after raw snapshots, process trace, exit codes, real JUnit, SHA-256 manifest of actual artifacts, and distinct **pytest outcome vs security verdict**. No test passes by merely detecting a mutation after an unauthorized effect has already been committed. Review by Agent C with adversarial negative vectors, not author self-approval.

## Decision gates
G1 isolation R-01: privileged local writer cannot mutate protected state inside declared boundary; any exceptions explicitly scoped.
G2 CB-05: authenticated external monotonic anchor, demonstrable Q1/Q6 rollback detection, bounded lag/fail-closed, independently verifiable evidence. Provider effect authenticity is a separate gate.
No merge, deployment, tag or publication. **PRODUCTION HOLD**.
