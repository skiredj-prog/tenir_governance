# P0 joint negative controls — executed GitHub CI evidence

**Date:** 2026-10-26  
**Repository:** `skiredj-prog/tenir_governance`  
**SUT SHA:** `e3a6e9243c2ffca4aeb70cd115843d44f2322513`  
**Branch:** `draft/g1-e8-g4a-p0-joint-controls-20261026`  
**Draft PR:** [#80](https://github.com/skiredj-prog/tenir_governance/pull/80) (stacked on Draft PR #79)  
**GitHub Actions run:** [40288518276](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288518276) — **both jobs succeeded**  
**Overall security verdict:** **R-01 WORKER-UID ONLY BOUNDED PASS; Q1/Q6 SYSTEM FAIL; R-01 PRIVILEGED OPEN; SYSTEM P0 OPEN; PRODUCTION HOLD.**

## Latest strengthened run — 22 worker-UID probes

The original 16-probe run remains an independently reproducible historical
record at the SHA above. The **latest security-strengthened SUT** is:

- **SUT SHA:** `c241ee5fa093429bf881c8246b4026cc0122cf98`
- **CI run:** [40288719509](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288719509) — **both jobs SUCCESS**
- **G4-A Q1/Q6:** `2 passed, 2 xfailed` (two **security failures reproduced**)
- **G4-A R-01:** `negative_probe_count: 22`, `worker_all_denied: true`,
  `same_uid_write_access: true`, positive control
  `RESERVED / execution_authorized=false`
- **JUnit and stdout:** [CI artifact 12635188162](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288719509/artifacts/12635188162)
- **OS probe JSON and stdout:** [CI artifact 12634804359](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288719509/artifacts/12634804359)

The strengthened R-01 probes now stage replacement payloads in an
**attacker-writable external temporary directory** before attempting
`os.replace` into the protected destination. They additionally check
creation denial for absent SQLite WAL, SHM and journal paths, quarantine
release-pending sidecar and both protected parent directories. The latter
checks require **EACCES or EPERM**; mere `ENOENT` does not count as
kernel permission denial. All 22 probes passed on the disposable runner.
The same-UID negative control continues to demonstrate residual
privileged DB write access.

**No new assurance level is implied:** the tested identities are on one
CI Linux host; G1 authenticated IPC and independent REG E8/CB-05
witness remain unintegrated.

## Provenance and exact observed results

| Test job | GitHub job ID | Observation | Interpretation |
| --- | --- | --- | --- |
| Actual G4-A Q1/Q6 counterexamples | [121457830265](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288518276/job/121457830265) | `2 passed, 2 xfailed` | Two tests **reproduced the unwanted admission** after local quarantine rollback/deletion; two strict XFAIL encode missing independent-witness denial |
| Actual G4-A OS worker/engine boundary | [121457830076](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288518276/job/121457830076) | `negative_probe_count: 16`, `worker_all_denied: true` | Disposable Linux runner: a distinct worker UID could not perform the attempted direct SQL, direct writes or file replacement against tested real G4-A DB/source/quarantine artifacts |
| G4-A positive control | same job | `operation_state: RESERVED`, `execution_authorized: false` | The **engine UID** could admit a simulated operation into the actual G4-A store; not provider dispatch |
| Same-UID negative security control | same job | `same_uid_write_access: true` | The engine UID can open the SQLite DB for writing. **Same-UID privileged compromise is not prevented** by the OS directory boundary |

### Evidence artifacts

- [Q1/Q6 JUnit + stdout + SUT SHA](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288518276/artifacts/12634354035)
- [R-01 JSON probe report + stdout + SUT SHA](https://github.com/skiredj-prog/tenir_governance/actions/runs/40288518276/artifacts/12634284280)

The R-01 JSON artifact includes per-probe path role, attempted operation,
return code and error details. The artifacts were uploaded by the workflow
with **21-day retention**. This document records the summary verified in the
GitHub logs; it does not replace the raw evidence. Exact environment,
package versions, UID/mount map and witness key provenance are **not**
fully captured yet for independent conformance.

## Attack-by-attack verdict

**Q6 — historical valid inactive quarantine restored:** current real
`ExperimentalG4AAuthority.admit_created()` accepts a new *simulated*
reservation after an attacker restores a previously authorized inactive
local quarantine file, despite a newer active incident. The positive
pre-attack control refused admission. **Security FAIL (P0).**

**Q1 — quarantine marker + initialized sentinel deleted:** current real
`ExperimentalG4AAuthority.admit_created()` accepts a new *simulated*
reservation after both local continuity artifacts disappear. The
pre-attack control refused admission. **Security FAIL (P0).**

**R-01 — separate worker UID:** all 16 tested direct SQL/write/replace
attempts were denied against the disposable **real G4-A store** with
protected directory ownership. The positive engine-UID admission worked.
**Bounded worker-only PASS**, not G1 authenticated-IPC conformance.

**R-01 — same-UID or root actor:** the engine UID can open the G4-A DB
read/write; no independent witness, hardware root, signed one-time grant
or tamper-resistant engine identity was tested. **SYSTEM P0 OPEN.**

## Failed preliminary CI runs (not silently counted)

Two earlier exploratory CI attempts are **excluded** from the final
evidence: one initially masked a subprocess failure via `tee` without
`pipefail`, and a subsequent attempt correctly failed because the
engine UID could not import code from the runner-private Git checkout.
The final successful harness stages a root-owned read-only copy of the
exact checked-out SUT package into an accessible temporary directory,
keeps protected SQLite/source directories inaccessible to the worker,
and propagates shell failures with `set -o pipefail`.

## Remaining exit criteria

- Single versioned interoperable E8/CMT wire protocol between REG #52
  and TENIR #79; cryptographic and scope binding tests.
- **Independent host/operator** witness with externally managed pinned
  public key and monotonic trusted head, including quarantine transitions.
- Real authenticated G1 IPC with signed, scoped, single-use grants and
  revocation, no alternative worker-reachable G4-A mutation path.
- Q1/Q6 attacks repeated against that full gate: **HARD_HOLD before
  actual G4-A reservation/settlement**, no provider effect.
- X01–X20 / INT-E8-01 crash, concurrency, replay, same-sequence
  substitution, root/key-replacement and ACK-loss matrix with raw
  evidence and external review.

**No production execution, release, merge or promotion authorized.**
`NEGATIVE_CONTROLS_EXECUTED / SYSTEM_P0_OPEN / PRODUCTION_HOLD`.
