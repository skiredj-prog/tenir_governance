# PROOF-01 — REG-RC-01 candidate + Proof Provider Fixture (B3 laboratory slice)

**STATUS: CANDIDATE / B3 LAB VERIFIED ONLY / B1-B2-B4 OPEN / PRODUCTION HOLD**

This delivery responds to the proposed REG-RC-01 candidate and the concrete B3 dependency of PROOF-01. It preserves three distinct truths: authorization, local state, provider-attested outcome. It does not claim TENIR-Gov end-to-end conformance.

## Contents

- `reg_rc_01/REG-RC-01_CANDIDATE_v0.1.md` — normative candidate, state dimensions, guards, failure semantics, RFC mapping, witness-first caveats.
- `reg_rc_01/RFC7_CANDIDATE_VECTORS.json` — 17 versioned adversarial **candidates** with honest implementation/pending dispositions.
- `proof_provider/fixture.py` — durable provider-only simulated effects / negative final tombstones (SQLite `BEGIN IMMEDIATE`, `synchronous=FULL`).
- `proof_provider/verifier.py` — independent *verification module*, pinned public key and checkpoint SHA required. Shares local canonical codec; not a second cryptographic implementation.
- `proof_provider/receipt.schema.json` — JSON Schema draft 2020-12 candidate for the signed receipt *body*.
- `tests/test_provider.py` — unit and negative tests.
- `docs/DECISION_AND_GATES.md` — B1–B4 readiness and nonclaims.
- `evidence/` — local test transcript and immutable hashes of the packaged sources.

## Run

Requires Python 3.11+ and `cryptography` (`pip install cryptography`). Tests: `./run_tests.sh` or `python -m unittest discover -s tests -v` from package root. Linux/POSIX tested locally. No external provider or network requests.

## Fixture semantics

- Repeated request with same `operation_id`, `idempotency_key`, action and scope is idempotent.
- Modified binding on the same operation ID is rejected.
- Simulated `execute(..., lose_ack=True)` **commits** the provider-side effect and its signed receipt, then raises `AckLost`, so querying the durable provider ledger is the only safe recovery path.
- `reconcile(operation_id)` returns `None` for no row: **UNKNOWN**, never proof of no effect.
- `finalize_not_executed(...)` atomically writes a signed terminal negative tombstone. Subsequent `execute` of that key is rejected.
- `export_bundle()` generates Merkle inclusion paths and a *provider-signed checkpoint*. A verifier **must** receive both the public key and the checkpoint SHA256 independently. A checkpoint bundled alongside the payload is NOT independent anchoring.
- Signature is Ed25519 over the package's own JSON canonical encoding (`sort_keys/separators`, UTF-8); **not RFC-8785/JCS** and not a claim of REG-6 conformity. Integers in minor units avoid float ambiguity.

## Replay versus proof

All tests passing does not close the red security vectors W1-BYPASS-01, W1-PRIV-01, Q1 or Q6 on direct paths. This component has no actual G4-A call, no separate signer OS identity, no real-world bank effects and no proof of cross-domain transaction atomicity. It is one dependency for a future E2E, not an E2E certification.

## Local lost-ACK demonstration and verifier CLI

```bash
PYTHONPATH=. python scripts/demo_lost_ack.py --out /tmp/reg-rc-demo
PYTHONPATH=. python scripts/verify_export.py \
  --bundle /tmp/reg-rc-demo/bundle.json \
  --public-key /tmp/reg-rc-demo/public-key.base64 \
  --checkpoint-sha256 /tmp/reg-rc-demo/checkpoint.sha256
```

The key/pin produced by the same demonstrator as the receipts do **not** constitute an independent authority or a separately pinned external anchor. To test a tampering rejection, change one digit in `checkpoint.sha256` and rerun verification. Do not package demonstration signing secrets with the output.
