# CB-05 external verifier pinning and trust continuity — v0.1
**Design only · 2026-10-25 · PRODUCTION HOLD**

## Triggering observations (same-operator reports, raw evidence not inspected)
- C1: a coherent rollback of local trust authority from revision 2 to revision 1 led to a newly `AUTHORIZED / COMMIT_AUTHORIZED` decision against the restored revision. The local receipt verified against the store-provided key.
- C2: edits limited to `roots_json`, removed triggers, or changed roots while retaining historical signed rows were detected. A **whole-file replacement** with a newly provisioned authority under an attacker-controlled root was reportedly accepted, and the store returned the attacker's receipt key as trusted.
- C3: static inspection of four `open`/`provision` entry points reportedly found no external pinned-profile digest parameter. This does not establish absence of pinning in all application layers.
- Operator notes earlier harness errors (wrong column name and trigger handling), corrected before the reported results. SUT SHA `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`; raw harness `/home/claude/r02/cb05_ep02/reuse_claims.py` and results `run/results.json` have **not** been received here. Reported partial SHA256s are not sufficient to verify file integrity.

## Decision: trust anchor must be external to the mutable store
A store must never nominate its own trusted root, receipt key, authority identity or trust generation. The verifier obtains a pinned `TrustProfile` from an **independent, authenticated provisioning channel** and persists the highest accepted profile generation and checkpoint head outside the store's rollback domain.

### Verifier pin record (conceptual)
```json
{
  "schema": "tenir.cb05.verifier-pin.v1",
  "realm_id": "realm",
  "store_id": "store",
  "authority_id": "authority",
  "trust_profile_id": "profile",
  "trust_generation": 3,
  "trusted_root_key_ids": ["root-2026"],
  "trusted_root_public_key_digests": ["sha256:<64-hex>"],
  "trusted_receipt_key_ids": ["receipt-2026"],
  "trusted_receipt_public_key_digests": ["sha256:<64-hex>"],
  "witness_id": "witness",
  "witness_public_key_digest": "sha256:<64-hex>",
  "minimum_checkpoint_sequence": 120,
  "minimum_checkpoint_digest": "sha256:<64-hex>",
  "pin_revision": 7
}
```
Values above are placeholders, not deployed pins. Pin integrity, signing, monotonic storage, authorization to rotate, disaster recovery and backup independence are mandatory implementation decisions.

### Verification order (fail closed)
1. Load verifier pins from an independently authenticated channel; refuse missing, untrusted, stale or conflicting pins.
2. Authenticate the **latest** witness head with pinned witness identity/key; verify realm, store, authority, trust generation, sequence, predecessor digest and quarantine state binding. An offline historical inclusion proof is not sufficient to establish current freshness.
3. Verify the store's claimed trust profile and receipt key against externally pinned profile and key digests; reject a new store's self-proclaimed roots or receipt keys.
4. Verify snapshot, authority decision and receipt signatures against **pinned keys** and bound domain, epoch, operation, payload and checkpoint.
5. Ensure local and witness state are consistent at the admission linearization point; unresolved quarantine or witness communication failures produce HOLD.
6. A valid historical signature cannot override a newer pinned revocation, minimum trust generation or witness head.

### Rotation and recovery
- Rotate roots/receipt/witness keys only via a separately authorized, signed transition with old-to-new continuity or explicit independent recovery authority. A new SQLite file or higher local revision is not rotation authorization.
- A root compromise is a distinct threat: signature validity alone cannot establish trustworthy provenance after compromise.
- If pins are lost, do **not** trust-on-first-use the restored local store. Require out-of-band recovery; HOLD until independent authority reestablishes pins and minimum head.
- A verifier must not accept a lower trust generation, checkpoint sequence, or conflicting same-sequence digest, including after its own restart.

### Negative acceptance tests
P01 C1 rollback of SQLite authority + sidecars after witness advances: HOLD before new admission.
P02 C2 complete replacement of authority DB with attacker-provisioned roots and receipt keys: pinned verifier rejects, even if local `open()` succeeds.
P03 C2 partial root edit: existing digest/guard protections still reject; not a substitute for P02.
P04 Valid old snapshot/receipt under revoked key: HOLD.
P05 Same-generation conflicting signed profile or checkpoint: CONFLICT/HOLD.
P06 Lost local pin cache, witness unreachable: HOLD, no TOFU.
P07 Witness replay/stale head and partition: HOLD for G2-required admissions.
P08 Legitimate signed key rotation with independent monotone pin update: acceptance only after durable verification.
P09 Fake higher trust generation signed by unpinned key: HOLD.
P10 Root compromise scenario: explicitly bounded residual risk and emergency revocation/recovery, no blanket G2 claim.

## Evidence intake
Add actual C1/C2/C3 harness, logs, JSON, JUnit (if generated), environment, complete SHA256 manifest and provenance to a **dedicated evidence branch**. Do not reconstruct raw evidence from narrative or claim independent review. The earlier R-02 archive is not present in this session; cannot modify it until supplied.

**Status: EXTERNAL PINNING SPECIFIED / IMPLEMENTATION AND INDEPENDENT VERIFICATION PENDING / PRODUCTION HOLD.**
