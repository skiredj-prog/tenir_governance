# CB-05 — Independent checkpoint and verifier: acceptance contract v0.1

Status: **DRAFT / RED-first / NOT IMPLEMENTED IN G4-A**. Baseline G4-A `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`. Current outbox hash chaining is internal to the same SQLite authority; it is **not** an independent anchor. No authenticated provider-effect evidence is claimed.

## Trust separation
Define an external checkpoint publisher/anchor and an independently operated verifier with a separate key/trust store and durable append-only checkpoint history. A checkpoint must bind `scope_id`, SUT commit, `authority_epoch`, `trust_generation`, `sequence`, `head_hash`, canonicalization profile/version, `issued_at`, issuer identity, signing key ID and signature; optionally previous checkpoint hash. Key validation, expiry, revocation, monotonicity and recovery are separate requirements. An external hash copy without authenticated independent control is insufficient.

## Acceptance criteria
- CB05-A: verifier independently authenticates checkpoint issuer/key and verifies canonicalized payload/signature.
- CB05-B: verifier detects modified events and observable gaps/rollback/forks **relative to a previously anchored checkpoint**.
- CB05-C: stale epoch, stale trust generation, replayed/older checkpoint and non-monotonic sequence are rejected or HOLD; no silent downgrade.
- CB05-D: unavailable anchor, missing checkpoint, revoked/unknown key or ambiguous trust state yields INCONCLUSIVE/HOLD; never PASS of exhaustiveness.
- CB05-E: independent inventory/witness is necessary to detect events omitted *before* capture; checkpoints alone cannot prove completeness or truth of external effects.
- CB05-F: all evidence explicitly identifies SUT SHA, checkpoint/verifier version, anchor identity, event-range, trust scope and observed vs unobservable failures.

## Negative test vectors
| ID | Injection | Expected oracle |
|---|---|---|
| CB05-N01 | Tamper with event already included in anchored head | Reject |
| CB05-N02 | Delete/rollback event sequence below previously anchored checkpoint | Reject |
| CB05-N03 | Replay valid older signed checkpoint against newer anchored sequence | Reject/HOLD |
| CB05-N04 | Sign with unknown or revoked key | Reject/HOLD |
| CB05-N05 | Present valid checkpoint for wrong epoch or trust generation | Reject/HOLD |
| CB05-N06 | Remove independent anchor/network | INCONCLUSIVE/HOLD, never PASS |
| CB05-N07 | Omit event before capture, no independent witness | INCONCLUSIVE (not detectable by chain alone) |
| CB05-N08 | Two conflicting heads for same scope/epoch/sequence | Reject/HOLD and record fork |

## Implementation boundary
No checkpoint API, independent signer, verifier, key lifecycle or third-party witness is present in the reviewed G4-A SUT. These vectors are **specifications**, not executed tests. Any future prototype must run in a separate disposable process/trust context and preserve the immutable G4-A snapshot. Signed checkpoint != signed provider receipt != external effect proof.

**Gate:** CB-05 remains GAP / NOT DEMONSTRATED until the negative tests are executed against an identified independently controlled anchor/verifier. No sectoral compliance inference.
