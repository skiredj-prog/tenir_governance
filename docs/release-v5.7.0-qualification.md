# TENIR-Gov 5.7.0 — Release qualification record (pre-tag)

Status: **NO-GO for tagging/publishing pending final verification**. This document is an evidence index, not an approval, certificate, or release announcement.

## Candidate

- Source branch: `main`
- Last verified merge commit: `992a42c2a5eed677494274f9c3688824c59004ef` (PR #53).
- `pyproject.toml` version: `5.7.0`.
- Merge sequence: PR #52 dependency audit in Raft/packaging preflight, then PR #53 README claim and provenance corrections.
- No `v5.7.0` tag, PyPI publish, or Zenodo version record is claimed by this document.

## Evidence available

| Control | Evidence | Assessment |
|---|---|---|
| PR #52 Governance CI | https://github.com/skiredj-prog/tenir_governance/actions/runs/40114242353 | Success on PR head |
| PR #52 Raft and packaging preflight (incl. pip-audit) | https://github.com/skiredj-prog/tenir_governance/actions/runs/40114242341 | Success on PR head after rerun |
| PR #53 Governance CI | https://github.com/skiredj-prog/tenir_governance/actions/runs/40116420411 | Success on final PR head |
| PR #53 Raft/packaging | https://github.com/skiredj-prog/tenir_governance/actions/runs/40116420215 | Success on final PR head |
| PR #54 Governance CI (qualification record) | https://github.com/skiredj-prog/tenir_governance/actions/runs/40118075516 | Success on documentation PR head before this evidence-only edit |
| On-main source and metadata | `pyproject.toml`, `CHANGELOG.md`, `CITATION.cff`, `.zenodo.json`, `README.md` | Version/claims aligned by source inspection |
| Release workflow gate | `.github/workflows/release.yml` | Tag `v*` triggers checks, artifact build, and downstream PyPI Trusted Publishing |
| Main push CI at exact merge SHA | GitHub Actions push run(s) for `992a42c2...` | **USER-OBSERVED SUCCESS**: operator reported all GitHub Actions checks green on main (2026-10-24); exact run URLs and independent API verification not yet captured |
| Exact tagged commit CI and distributions | Pending, no tag created | **NOT VERIFIED** |

## Explicit scope and residual risks

- Single-host deployment profile only. Production requires verified deployment manifest and durable persistence.
- FM-20 remains open: PySyncObj pickle transport has not been remediated. Multi-host/Raft production operation is blocked; no C3 physical multi-host guarantee.
- No exactly-once external provider effect, independent certification, or Byzantine-fault-tolerance claim.
- Synthetic or loopback Raft tests are not multi-host production evidence.
- A clean PR run does not establish a clean push run on `main` or a clean tagged build.

## Release decision gate

Before considering tag `v5.7.0`:
1. Capture GitHub Actions `push` workflow run URLs and conclusions on exact `main` SHA (including governance and Raft/packaging). Operator reported success on 2026-10-24; independent connector verification remains outstanding.
2. Ensure the release workflow's tag/version/changelog gate and dependencies/audit are suitable for the same source SHA; confirm required PyPI publishing environment and approval route.
3. Check changelog, Security Policy, DOI and public claims remain bounded and distinguish source line from published release.
4. Obtain an explicit publication decision recognizing that pushing a `v*` tag initiates a publication-capable workflow.
5. After authorization only: create tag on approved immutable SHA, track release workflow, inspect produced artifacts and PyPI result; separately verify any GitHub/Zenodo release record rather than assuming one exists.

**Operator attestation (2026-10-24):** all post-merge checks on GitHub were observed green, including Raft regressions and Python packaging preflight. This is a user-observed status, not an independently retrieved run result. The checks associated with a future tag have not executed.

This record intentionally does not claim that steps 1–5 have completed.
