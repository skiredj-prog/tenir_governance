# R-01 five-vector report — evidence triage (2026-10-25)

Status: **UNVERIFIED REPORT / NOT AN EXECUTED CAMPAIGN ATTESTATION**.
Baseline claimed: `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`. The source is a user-supplied narrative and explicitly *simulated* JUnit. No raw runner output, database, checkpoint or independently hashed artifacts were provided for this five-vector campaign. Keep separate from actual R-01 PR #75 evidence.

| Vector | Submitted claim | Admissible evidence status |
|---|---|---|
| 1: raw SQL / function injection | ordinary SQL prevented before commit | Previously supported narrowly by PR #75 R01-N01/N02; cannot attribute to pre-flight interceptor without trace |
| 2: trigger DROP | committed privileged schema mutation, detected on reopening | Consistent with PR #75 R01-N04; cite that campaign, not the simulated JUnit |
| 3: physical DB rollback | rollback succeeded and external checkpoint detected divergence | **INCONCLUSIVE / NOT VERIFIED**; no authenticated independent checkpoint or raw observation furnished |
| 4: epoch spoof | crypto authority binding not implemented | **BLOCKED / NOT IMPLEMENTED**; simulated JUnit incorrectly marks testcase as passed |
| 5: concurrent privileged writer | ordinary writer blocked, privileged writer committed | Privileged write is previously demonstrated by PR #75 N03/N04; concurrency/interleaving **NOT VERIFIED** in supplied report |

## Artifact validity
- XML is explicitly labeled **simulated**, and its pass/fail encoding does not represent the BLOCKED vector.
- Claimed database SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` is SHA-256 of an empty file; not proof of an initialized SQLite database.
- Other supplied hash strings are not backed by artifacts; do not cite as verified digests.
- No actual test commands, exact environment, checkpoint identifier, event observations or per-vector raw results attached.

## Conditions for promotion
Run on a disposable POSIX lab with exact SHA; save raw stdout/stderr, real JUnit, SQLite pre/post snapshots, checkpoint provenance, exit codes, actual computed SHA-256 manifest, and distinct verdicts PASS/FAIL/INCONCLUSIVE/BLOCKED. Independently review before making any assurance claim.

PR #73 and PR #75 remain unaffected by this triage branch. **PRODUCTION HOLD.**
