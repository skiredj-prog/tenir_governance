# JW-09 / JW-10 — laboratory qualification candidate

**Baseline (parent)**: `c23f9d61c3fa03e81432d766c05fc747b904fa61` (JW-08, verified as GitHub ancestor).
**Candidate implementation**: `d88e0e94779f914a7f6ddb6a008427a717dec54f`.
**Requested topology name**: `LAB-TENIR-GOV-PROD-CANDIDATE-09` — **not attested** by this PR.
**Actual code topology**: `lab-s1-native-sqlite-single-host-2026-10-28` (constant `TOPOLOGY_ID` in `native_positive_permit.py`). Kernel and Witness remain separate SQLite databases.
**Qualified verdict**: `SCOPED_JW09_JW10_TEST_PASS`; aggregate `JW01_TO_JW10_VERIFIED` **NOT CLAIMED**.

## Changes

JW-09:
- After a Witness checkpoint has been committed for a subject/fingerprint, `IndependentWitness.issue()` reuses the stored record and its original execution_id/claim_token; a new proposed identity cannot replace it.
- Divergent subject/fingerprint is refused; a historical checkpoint without execution identity is not silently upgraded to an identity-bearing Witness.
- `NativePositivePermitAuthority.consume_and_claim()` checks the Witness subject, fingerprint, execution identity and claim token before an outbox claim is persisted. Missing binding fails closed, never repaired with a newly generated ID.
- No two-database atomic commit. An orphan Witness may exist if the Kernel rolls back. Real child-process crash test proves the Kernel outbox remains absent and the same Witness is recovered on restart.

JW-10:
- Opt-in `verifiable_recovery=True` requires explicitly injected 32-byte raw Ed25519 secrets (via configured environment paths), and persists the **public-key** fingerprint in the independent Witness database and the paired public-key fingerprints in Kernel SQLite. No secret key bytes are written to those tables.
- Restart with a different Witness or Kernel key fails before use. The default ephemeral laboratory path remains available but **cannot claim verifiable recovery**.

## CI / evidence

- [GitHub Actions #40820626516](https://github.com/skiredj-prog/tenir_governance/actions/runs/40820626516): **49/49 PASS**, including 7 JW-09/JW-10 scoped cases, JW-08 command-binding and JOLT audit regressions.
- The test process makes a real `os._exit(86)` between the Witness SQLite commit and Kernel `positive_claim` commit, verifies **one Witness checkpoint, zero Kernel outbox entries**, reinitializes both authorities with the same injected keys, and confirms the original Witness is replayed.
- CI checks that the baseline SHA is an ancestor, not that the changed candidate commit is byte-for-byte equal to the baseline.

## Explicit limits and next gates

- This demonstration does not prove resistance to rollback of **both** independent SQLite databases or to privileged substitution of trust material. Public key fingerprints in locally writable tables are not an external monotonic trust root.
- Binding verifies Witness evidence within the scope of the native claim; it is not a cryptographic proof of exactly-once effects at a remote provider.
- For an institutional JW-10 contract, require out-of-band provisioned key identifiers, approved key rotation/revocation, offline custody backup and independent monotone anchoring.
- The requested topology `LAB-TENIR-GOV-PROD-CANDIDATE-09` and the aggregate JW-01..JW-10 `VERIFIED` verdict need a separate exact-head, exact-topology end-to-end run with raw artifact hashes and no excluded mandatory gates.
- PR remains Draft. No merge, deployment, production cutover, REG+ promotion, or global certification.
