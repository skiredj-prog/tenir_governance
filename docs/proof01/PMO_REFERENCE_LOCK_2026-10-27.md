# PROOF-01 — PMO reference lock (2026-10-27.1)

**Decision:** `SOURCE_REFERENCE_PINN / INTEGRATED_SUT_PENDING / PRODUCTION_HOLD`
**Owner:** Agent 0 / PMO. **Status:** review candidate, non-certifying. **Issue:** [#82](https://github.com/skiredj-prog/tenir_governance/issues/82).

## 1. Verified GitHub source candidates — distinct branches, NOT one SUT

| Component | PR | Source SHA | Status |
|---|---|---|---|
| G4-A simulated atomic lab | [TENIR #73](https://github.com/skiredj-prog/tenir_governance/pull/73) | `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3` | Draft |
| G3.1 authority fence | [TENIR #72](https://github.com/skiredj-prog/tenir_governance/pull/72) | `4c422b559b4d7e7b639eff275919b9d38484d1df` | Draft |
| W3 negatives Q1/Q6 R-01 | [TENIR #80](https://github.com/skiredj-prog/tenir_governance/pull/80) | `8f9d8863fe5887741963192044ca6987f3ec55ea` | Draft |
| E8 witness experiment | [REG #52](https://github.com/skiredj-prog/reg-conformance/pull/52) | `62ef6f30efd27003d654758b7fe8e2f885356da2` | Draft |
| W4.2 synthetic semantic verifier | [REG #55](https://github.com/skiredj-prog/reg-conformance/pull/55) | `b9be88341c23438a8e553ea64a4e6d55ea96faee` | Draft |
| W4.3 GitHub synthetic differential oracle | [REG #57](https://github.com/skiredj-prog/reg-conformance/pull/57) | `cd0090a8b5ead41ecfe6721474a7673afd8b33e2` | Draft, stacked on #55 |
| REG-ARCH v0.4.1 candidate | [REG #50](https://github.com/skiredj-prog/reg-conformance/pull/50) | `925f8b1e79f27033bee69ac96960a4c686467a3e` | Draft, not on critical path |

**Do not set the E2E integration SHA to any row above.** The combined G4-A/B3/RC-01/I8 implementation is **not yet identified by a verified source commit and deployment manifest**. It is an entry requirement of W2 issue [#81](https://github.com/skiredj-prog/tenir_governance/issues/81).

## 2. Versioned interface lock

- Shared packaging **CEB-01**: `ceb-01/0.1` candidate, `proof-manifest-v1.json`: `proof01-w4/1`.
- Native B3 provider bundle: `proof-provider-bundle/candidate-v0.1` (lab only).
- W4.2 synthetic event format: `proof01-signed-events/1`; **not wire compatible by assertion** with native B3, and no re-signature may manufacture evidence.
- Reconciliation `REG-RC-01`: candidate v0.1, not ratified.
- RFC-0..7 frozen draft baseline: no normative changes implied.
- **The local 51-test native W4.3 kit is NOT automatically part of GitHub PR #57.** Compare sources before attempting merge/promotion.

Any incompatible interface change => new version and PMO + producer/consumer review.

## 3. Deployment reference profile, NOT a deployed system

Proposed lab: Linux x86_64 single-host POSIX; Python 3.12.x with exact patch pinned at actual run; SQLite exact version/journal/fsync recorded; simulated durable provider **only**. Capture OS/kernel, UID/GID/groups, FS and volume mounts, network/credentials, container image digests, key provenance/custody, environment lockfile, `tau_K` SLA derivation, fail policies and clock. Include direct provider/DB bypass, same-UID privileged writer, Q1/Q6 rollback and crash in threat model. `SIGKILL` does NOT establish real power-failure durability.

Required 3-part deployment identity for all W2/W3/W4 evidence bundles:
1. **same full 40-hex integration git SHA** (actual G4-A adapted to B3/RC-01/I8);
2. **same SHA-256 of deployment manifest** (images, settings, topology and authority);
3. **same topology ID** (separate `test_instance_id` for destructive W3 replicas).

**Until those 3 fields have been confirmed by Agent 1, the common system gate is HOLD.** Individual test PASS ≠ integrated proof.

## 4. Artifact contract: W2, W3-B1, W3-B2, W4

Each owner delivers a **CEB-01 v0.1** bundle:

```text
proof-manifest-v1.json          # SHA-256 inventory
convergence.json               # source SHA/deploy digest/topology/instance/fault/trust
reports/test-results.json      # test result AND independent security verdict
reports/proof-obligations.json # OPEN/REFUTED/CLOSED_WITHIN_SCOPE/INDETERMINATE
evidence/**                    # ORIGINAL native records, receipts, logs, witness, budgets
```

Public trust pins and custody declarations are referenced **outside** the bundle. W4 verifies but never creates or re-signs the producer records. `TEST_PASS` may coexist with `SECURITY_FAIL` when an attack successfully reproduces a bypass. No claim of unknown external effect completeness from local signatures or hashes.

## 5. Ownership / noninterference

- **Agent 1 W2** — only integration adapter, native proof export, I8/RC-01 lifecycle, real G4-A path. Consumes witness contract. Delivery: exact integrated SHA, deployment manifest, REPAIR crash trace, I8 and provider artifacts.
- **Agent 2 W3-B1** — R-01, direct SQL/API/provider path attacks, process privileges, IAM and network. No editing Agent C witness protocol.
- **Agent C W3-B2** — witness protocol, pin provenance, Q1/Q6 erasure/rollback, crashes. No editing G4-A grant/dispatch or I8.
- **Agent 3 W4** — independent verifier/oracle and RFC-7 candidate corpus. No mutation or re-signing of source evidence.
- **Agent 0 PMO** — SHA locks and version guards, review of shared interfaces, integrated gate and issue backlog.

Convergence: W2 emits genuine E2E evidence, W3 attacks that *same code/deployment* (possibly destructive replicas), W4 evaluates both positive and negative evidence, PMO publishes bounded disposition.

**Production HOLD:** B1/B2/R-01/Q1/Q6 still open, external effect observation completeness and independent trust custody unproven. No merge/release/certification authorized by this note.

## 6. Reproducible PMO reference package

A local offline package **`PROOF01_PMO_reference_lock_2026-10-27.zip`** is assembled with `source-lock.json`, `environment-profile.json`, `artifact-register.json`, offline checker, 11 unit tests, all seven source ZIP inputs, and SHA256SUMS. **ZIP SHA-256:** `3d4ecb30a066d68134a9642a8668ed75a65be2982ffaeb40b2088648730b8d4b` (source digest, not a GitHub-hosted release asset). All 11 tests pass after extraction; validator reports `reference_validation=PASS` and `convergence_gate=HOLD`. The package is retained in the ChatGPT working artifacts, not uploaded to GitHub as binary.

**Next PMO action:** require W2 to pin the true integration SHA + OCI/deployment digest; only then turn this **source** reference into an actual **system** convergence lock.