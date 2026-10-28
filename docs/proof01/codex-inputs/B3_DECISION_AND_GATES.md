# PROOF-01 — Decision and engineering gates

**Decision:** Accept user-proposed REG-RC-01 as candidate baseline; apply the clarifications documented in the spec; implement only the B3 fixture in this tranche. Do NOT modify frozen RFCs. W1 counterexamples are versioned as OPEN obligations, not claimed closed.

| Gate | What this tranche does | Status |
|---|---|---|
| B1 external effect causally dependent on G4-A | RFC-7 candidate adversarial vectors; no mandatory integration | OPEN |
| B2 Q1/Q6 and external monotone witness | Contract for witness-first recovery; no proof of distributed atomicity | OPEN |
| B3 external receipts and lost ACK | Durable SQLite effect ledger; signed Ed25519 provider receipts; signed Merkle checkpoint; independent verifier; idem/tombstone | **LAB FIXTURE ONLY** |
| B4 independent audit | Verifier code independent from provider implementation; pinned verification inputs explicit; no independently provisioned signer/witness/second implementation yet | PARTIAL |

## Nonclaims

- Signed Merkle checkpoint is **not** independently anchored unless an actual auditor pins it outside provider control.
- Current provider and verifier reuse the same canonical encoding module. This is **not** independent differential cryptographic implementation; B4 remains partial.
- Fixture isn't an external financial provider, doesn't exchange mTLS messages or invoke the true G4-A authority, and has no isolated OS identity.
- A signed `EXECUTED` receipt verifies a ledger assertion, **not a real-world transfer**.
- The fixture does not create an operational `REPAIR` state or local I8 budget settlement. These are still P0 E2E work.

## Tests which are implemented

See `tests/test_provider.py`, including post-commit lost ACK, persistent restart, multi-process same-key race, signature tampering, changed request, final no-effect tombstone, Merkle path/root manipulation, stale receipt, wrong provider identity, and independent pin mismatch. `run_tests.sh` is the re-entry point.

## Next gates

1. Wire a **mandatory** G4-A owner with provider endpoint credentials and defined OS boundary. Probe direct G4-A admission, privileged writer, and unauthenticated alternate provider requests (W1-BYPASS-01, W1-PRIV-01).
2. Develop local reconciliation state machine and I8 settlement with versioned CAS, persist evidence BEFORE claiming terminal; prove `REPAIR` under crash.
3. Isolate fixture signer and witness in distinct principal; publish externally pinned roots in a reproducible lab harness.
4. Add a second genuinely independent verifier (different canonicalization/crypto implementation) and bind all proof artifacts to fixed commits, manifest, deployment identities and attack runner.

**Commercial pilot: HOLD. Runtime/REG conformity: NOT ESTABLISHED.**
