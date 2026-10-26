# G1-IPC experimental prototype

**Not production ready.** Local SQLite-only demonstration. No G1 OS-isolation proof, no G2 witness, no real grants, no STATUS endpoint, no admission/quarantine integration, no external provider effects. The `auth.grant_signature` field MUST equal `NOT_IMPLEMENTED` and is **not validated as a grant**. Do not expose to untrusted clients or use for real workloads.

Python 3.12+, `cryptography`, `pytest` for tests.

`python -m pytest -q test_g1_ipc.py`

Server (only in a disposable test environment):
`python g1_ipc.py --db /protected/g1.db --socket /ipc/g1.sock --public-key /protected/worker.pub --worker-uid 1002`

Public key file contains 32 raw Ed25519 bytes; provision externally. Socket directory ownership and cross-UID access must be configured separately. Server refuses to unlink existing socket paths. Envelope signatures use `Ed25519.sign(b'TENIR-G1-IPC-ENVELOPE-v0.1\\x00' + canonical({header,auth,payload}))`. Client and signing code are currently demonstrated in the test module, not a deployed worker client.

**Limitations:** unsigned/unauthorized grant semantics, fixed worker UID, no expiry enforcement, no daemon restart orchestration, no `STATUS`, no admission prerequisite, no quarantine/epoch authority synchronization, no remote side-effect guarantees. This is a bounded proof-of-mechanics only.

## Verification provenance
The three tests passed locally on 2026-10-25 (`python -m pytest -q test_g1_ipc.py`, 3 passed in 0.06s). This was **not** a cross-UID sandbox run or GitHub CI run. Do not infer G1 security assurance from these tests.

## INT-E8-01 red-first tests (PR #77 ↔ PR #79)

Run `python -m pytest -q test_g1_e8_integration.py` from this directory.

- Four **MOCK_ORACLE_ONLY** tests exercise a test-only pre-authorization shim and SQLite state checks; they are not proofs of witness independence, CMT signature correctness or daemon integration.
- Two **XFAIL(strict=True)** tests call the real experimental `g1_ipc.process` and describe the presently missing E8 enforcement. XFAIL is a recorded missing capability, not a conformant PASS.
- No local production code, schema or real provider effect is changed. The existing `process()` routine will still commit under a rolled-back or divergent state because it has **no witness parameter**.
- The normative `INT-E8-01` contract, including the eight negative/positive sub-vectors and required independent evidence, is in `docs/g4a/G1_E8_CB05_G4A_JOINT_CONFORMANCE_MATRIX_v0.1.md` on PR #77.
- The mock does not reserve or consume one-time CMTs. Before converting XFAIL to mandatory integration PASS: provision distinct hosts and key pins, implement authenticated compare-and-advance, grant revocation and durable status reconciliation, instrument provider dispatch and crash barriers.

**Status:** test code committed, execution/CI evidence not established, E8 integration not implemented, PRODUCTION HOLD.

## E8/CB-05 experimental pre-authorization — signed loopback, NOT independent

Added:
- `e8_lab.py`: test-only real HTTP server on **127.0.0.1**, SQLite-backed monotone compare-and-advance, Ed25519 engine request authentication, signed witness responses, scoped CMTs, strict request-ID replay/conflict handling, independently supplied public-key pin *within the test*.
- `g1_e8_guarded.py`: separate experimental G1 Unix-socket daemon and `process_guarded()` path; witness approval precedes business rows, the local head is updated in the same SQLite transaction as local settlement, and authenticated refusal creates a durable security HOLD.
- `test_g1_e8_live.py`: loopback HTTP/SQLite/Ed25519 tests for stale epoch, same-epoch head mismatch, transport failure, approved CMT, wrong witness key, simulated crash before SQLite commit, idempotent replay and actual Unix-socket request. The previous **XFAIL** for privileged business-table tampering has been promoted to a passing regression test after a canonical live-state digest was added. Other unimplemented security boundaries remain RED.
- `.github/workflows/g1-e8-experimental.yml`: scoped exploratory CI; it must never be represented as an independent-security gate.

Run locally with Python 3.12+:
```sh
cd experimental/g1_ipc_prototype
python -m pip install pytest cryptography
python -m pytest -q -ra test_g1_ipc.py test_g1_e8_integration.py test_g1_e8_live.py
```

### The security boundary is NOT complete

1. Witness and engine share the SAME host in these tests. The lab server deliberately refuses non-loopback destinations and provides **no TLS/mTLS**. Separate hosts, operator identities, independently managed roots and provisioned key pins are mandatory for CB-05 qualification.
2. **P0 third-failure lab mitigation:** `g1_e8_snapshot.py` deterministically reconstructs the *actual G1 prototype* `g1_local_commits` and `g1_idempotency_ledger` rows and table definitions, checks them against the cached attested `state_digest` before replay or new admission, predicts the post-commit digest prior to witness CAS, and rechecks actual SQLite state before COMMIT. Witness-signed CMTs bind `next_state_digest`. Deletions, unauthorized row edits, schema changes, replay-after-tamper and a locally forged cached head are now tested. This **does not** cover all real G4-A tables, authoritative quarantine sidecars, code/trigger replacement, or the case where the same privileged attacker controls the running engine and its signing credentials. Thus **R-01/Q1/Q6/C1/C2 remain OPEN in the full threat model**.
3. `auth.grant_signature` still equals `NOT_IMPLEMENTED`. The hash of this placeholder in a CMT is not a valid grant. No issuer standing, one-time authorization, cancellation/epoch revocation or last-moment dispatch check exists. The lab never dispatches provider effects. **R-01 is not closed**.
4. Witness CAS and local COMMIT cannot be atomic together. A test-only injected exception after witness advance is recoverable by retrying the exact request, but this is **not** a SIGKILL crash campaign. An uncertain response induces `HOLD_GLOBAL` requiring reconciliation. Full recovery, STATUS, TTL semantics, trust rotation and external effect idempotence remain unimplemented.
5. This lab mutates only the experimental **G1 prototype tables**, not the real G4-A settlement transaction or production runtime. The ordinary `g1_ipc.py` prototype remains available without E8; the new `g1_e8_guarded.py` is a separate opt-in experimental path. No production enforcement is introduced.
6. Witness response signatures are checked against a client-provided public key, but there is no production-grade external pin provisioning, transport authentication, audit trail, or tamper-resistant witness operator.

### Evidence classification

`MOCK_ORACLE_ONLY` (older shim) < `LOOPBACK_CRYPTO_SQLITE` (this test) < `SEPARATE_HOST_ADVERSARIAL` (NOT DONE). The first two are explorations only; **INT-E8-01 normative PASS is unearned**. Red-first XFAIL is an open finding, not success.

**PRODUCTION HOLD · P0 OPEN · PR #77 normative matrix unchanged by this implementation.**

### P0 third-failure remediation evidence — 2026-10-26

- `g1_e8_snapshot.py`: fixed-schema, canonical actual business-row digest, no reliance on cached `head.state_digest` for local truth.
- `g1_e8_guarded.py`: pre-admission check **before idempotent replay**, and post-insert verification in the same SQLite transaction.
- `e8_lab.py`: externally signed CMT now binds the computed **next** state digest; E8 witness advances to this digest instead of to a synthetic hash-of-intent.
- `test_g1_e8_live.py`: prior explicit RED test `test_real_engine_must_detect_business_db_tamper_even_when_cached_head_untouched` now PASS; adversarial modification of ledger, business rows, schema, and cached head tested. New trusted genesis initialization rejects default all-zero head.
- Verified GitHub Actions run: https://github.com/skiredj-prog/tenir_governance/actions/runs/40269372473 on SHA `3fcae6b7da7d162e776b0dd29055b237d20cf1da`: **24 passed, 2 xfailed**, no unexpected errors. This is an exploratory loopback-only CI result, not independent E8 or G4-A qualification.

**Assurance decision:** `LOCAL_G1_BUSINESS_DIGEST_TAMPER_DETECTED` for the defined test surface only. `SYSTEM_P0_OPEN / PRODUCTION_HOLD`. The remaining 2 XFAIL are the unguarded historical `g1_ipc.process` path, which still bypasses E8 entirely.
