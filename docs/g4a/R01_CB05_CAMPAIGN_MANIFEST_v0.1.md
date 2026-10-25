# R-01 + CB-05 — Negative campaign manifest v0.1

Status: CONTRACTS COMMITTED / SUT NEGATIVE EXECUTION PENDING.

Source SHA: `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3` (G4-A experimental scoped CI PASS). R-04: CLOSED for its experimental contention test. CB campaign on `7422423` remains a distinct historical result.

## Execution discipline
1. Freeze a disposable POSIX single-host SUT and independent tester identities.
2. Capture exact Git SHA, build, environment, DB ownership, writer inventory and pre-test snapshots.
3. Run R01-N01..N06 with privileged tests in disposable databases only. Record actual committed mutation separately from subsequent fail-closed detection.
4. Do **not** run CB05-N01..N08 as if integrated: first implement an independently controlled checkpoint/verifier prototype on a new branch with a distinct SHA. Until then mark BLOCKED / NOT IMPLEMENTED.
5. Persist raw observations, stdout/stderr, SQL and process exit codes, hash manifest, JUnit/JSON summaries, and reviewer attribution.
6. Distinguish PASS (demonstrated property), FAIL (counterexample), INCONCLUSIVE (insufficient witness), OUT_OF_SCOPE (predeclared exclusion) and BLOCKED (capability unavailable). Do not count oracle self-tests as SUT tests.

## Decision record
- R-01: known privileged-write counterexample; boundary cannot be widened without new enforcement.
- CB-05: independent anchor absent; verification guarantee unproven.
- No merge, production promotion, provider dispatch, sectoral certification or release authorized.
- PR #73 remains Draft; no changes to its head from this branch.
