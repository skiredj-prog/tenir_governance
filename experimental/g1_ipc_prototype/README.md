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
