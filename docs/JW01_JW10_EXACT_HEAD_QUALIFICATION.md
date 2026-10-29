# Exact-head laboratory JW-01 — JW-10 qualification

**Candidate lane:** independent audit branch stacked on [PR #98](https://github.com/skiredj-prog/tenir_governance/pull/98); **Draft, no merge/release/cutover.**

## What is checked

The qualification harness `audit_tests/qualify_jw01_jw10.py` records the **exact checked-out HEAD commit** and verifies that pinned JW-08 source `c23f9d61c3fa03e81432d766c05fc747b904fa61` is an ancestor. It executes a single pytest process over:

- `audit_tests/test_jolt_safety_contract.py` — original adversarial contract.
- `w2_integration/test_native_command_binding.py` — canonical command and provider binding.
- `audit_tests/test_jw09_jw10_recovery_custody.py` — orphan Witness recovery after child `os._exit(86)`, exact subject/fingerprint and explicit injected key custody on restart.
- `audit_tests/test_jw_joint_rollback_limit.py` — mandatory falsification tests, including joint Kernel/Witness rollback and one-sided Witness rollback.

The machine-readable report binds **named test methods** to each JW-01..10 label and marks `SCOPED_TEST_PASS` only if each method is present and passes, including parametrized cases. This is a *test evidence mapping*, **not proof that every normative requirement under each JW label is satisfied**.

## Topology and requested ID

- Requested label: `LAB-TENIR-GOV-PROD-CANDIDATE-09`.
- Actual code `TOPOLOGY_ID`: `lab-s1-native-sqlite-single-host-2026-10-28`.
- Data stores: distinct local SQLite Kernel and Witness files (WAL); one host, local file and key custody.
- Keys: fixed lab-only environment-injected Ed25519 material under `verifiable_recovery=True`, fingerprints persisted locally; no external anchor.
- The requested and actual topology IDs **do not match**. The audit does not rewrite the production code's ID or claim validation of the requested topology.

## Immutable report structure

The CI archives:

- `pytest-raw.txt`: full pytest output and invocation.
- `pytest-junit.xml`: raw machine-readable test cases.
- `qualification-result.json`: exact HEAD and JW-08 ancestor, per-JW evidence mapping, negative controls, source-component file hashes and explicit verdict.
- `topology.json`: requested vs implementation topology and guarantee boundaries.
- `dependencies-freeze.txt`: installed package versions.
- `evidence-sha256.json`: SHA-256 checksums of all files above (not recursive).

GitHub Actions provides a separate archive digest once the run uploads the files. Evidence is for the candidate SHA *actually checked out* in CI; the baseline SHA is not incorrectly presented as the execution source.

## Required negative control: joint rollback

An adversary who has full restoration control over **both** local SQLite stores can roll Kernel and Witness back to an earlier mutually consistent checkpoint. Without an **independent monotone anchor**, locally stored key fingerprints and signatures do **not** prove the current data is the latest. The test explicitly reproduces this and must pass as an **EXPECTED_SECURITY_LIMIT**, not as evidence of anti-rollback certification. Witness-only rollback is separately detectable when a newer Kernel claim is preserved.

No external provider is invoked in this test. It does not claim exactly-once effects or causal end-to-end correctness.

## Verdict policy

Even with all mapped unit/integration tests passing, the aggregate verdict is **`HOLD_NOT_VERIFIED`**, not `VERIFIED`, because the requested topology differs, rollback of both stores is demonstrably not detected, there is no independent external monotone trust anchor or distributed fencing, and production effects/claim authority have not been independently qualified. Any missing mapped test or failing pytest returns `TEST_FAILURE` and fails CI.

Full JW-01..10 normative qualification would require a reviewed requirements matrix with complete coverage, independent witness/anchor and trust-key custody, original external-effect evidence and an exact deployment topology. No merge or REG/REG+ claim is authorized by this lane.
