# PROOF-01 — Codex developer handoff (28 October 2026)

**CANDIDATE SOURCES — not integrated G4-A; no certification or production release.** This branch is stacked on PMO Draft PR #83. GitHub documentation is the reference for interfaces; archived executable lab sources remain distinct from current TENIR-Gov runtime.

## Source kits (exact local artifacts)

| Package | SHA-256 of ZIP | Scope |
|---|---|---|
| PROOF01_REG_RC01_B3_fixture_candidate_2026-10-26.zip | `72aa1dd690ac2b2bf91fd4876ea48ac243108a541c6d211762664c676dbad3cd` | Candidate B3 provider and REG-RC-01 specification |
| PROOF01_B3_RC01_I8_REPAIR_verified_2026-10-26.zip | `dbd80d3e1f24ecde572534623664550f829e5b48a9392fda69b913ef13e2e272` | B3 + local I8/RC-01 REPAIR laboratory, 58 PASS / 4 SKIP OPEN |
| PROOF01_W43_native_B3_differential_verified_2026-10-27.zip | `6da2426980c4f7b2f44f2bbc013f16fd873e235773637d75876524db3f201252` | W4.3 native B3 adapter and independent differential oracle, 51 lab tests; **not identical** to REG GitHub PR #57 |

**Important:** these original ZIP files are not GitHub release assets and are not included as binaries in this source-docs PR. Local extraction is required to execute the original test harness and recover the entire Python source. Do not infer their availability from the GitHub document links. Never treat a component kit hash as the integrated SUT git SHA.

## Documents

- [B3 fixture README](B3_FIXTURE_README.md) and [B3 gates](B3_DECISION_AND_GATES.md).
- [B3 ↔ RC-01 ↔ I8 REPAIR laboratory README](REPAIR_LAB_README.md) and [open obligations](REPAIR_OPEN_OBLIGATIONS.md).
- [REG-RC-01 Candidate v0.1](REG_RC01_CANDIDATE_v0.1.md) — unratified normative candidate.
- [W2 Codex integration instructions](W2_INTEGRATION_HANDOFF.md) — authoritative target branch, contracts, receipt preservation and CEB-01 acceptance.
- PMO source lock and independent witness addendum in `docs/proof01/` of this branch.
- W4.3 native adapter documentation in REG Conformance's separate docs branch and Draft PR (do not conflate with synthetic #57).

## Correct workflow

1. Checkout `skiredj-prog/tenir_governance` G4-A [PR #73](https://github.com/skiredj-prog/tenir_governance/pull/73), SHA `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`. This is **component** baseline, not E2E.
2. Extract original REPAIR/B3 ZIP in a disposable POSIX Linux or WSL2 lab; verify SHA-256 and rerun suite, preserving original output.
3. Implement the *real G4-A path* to provider B3 with a single owner for dispatch and I8 budget. Do not introduce duplicate budget authority; do not re-sign B3 evidence.
4. Produce one new **integrated git commit SHA**, one deployment manifest digest + topology ID and a CEB-01/0.1 source evidence bundle. Submit via [Agent 1 #81](https://github.com/skiredj-prog/tenir_governance/issues/81#issuecomment-6432389680).
5. W3 [#74](https://github.com/skiredj-prog/tenir_governance/issues/74)/[#76](https://github.com/skiredj-prog/tenir_governance/issues/76) adversarial tests and W4 [REG #56](https://github.com/skiredj-prog/reg-conformance/issues/56) then inspect **that same commit, deployment and evidence scope**. Positive lab PASS cannot close B1/B2/Q1/Q6 or independent external-effect completeness.

The W4.4 source PRs #58/#59 are Draft and have known test and verification/archiving gaps. Do not promote them into E2E assurance.

**Source Lock:** pinned in Draft PMO #83; **Integration Lock:** PENDING; **W3 IND-M1 independence:** HOLD; **E2E & production:** HOLD.
