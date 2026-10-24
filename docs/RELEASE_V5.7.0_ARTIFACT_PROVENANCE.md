# TENIR-Gov 5.7.0 — PyPI artifact provenance gate

**Status:** evidence checklist, not a completed attestation.  
**Git tag:** `v5.7.0`  
**Source SHA:** `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`  
**Published GitHub release:** https://github.com/skiredj-prog/tenir_governance/releases/tag/v5.7.0  
**PyPI package:** https://pypi.org/project/tenir-governance/5.7.0/

## What is verified from the tagged release workflow

The tag-triggered `.github/workflows/release.yml` verifies that the release tag version matches `pyproject.toml`, that the tagged commit is an ancestor of `main`, and that `CHANGELOG.md` includes the corresponding release entry. On Python 3.12, its build job installs `.[api,test,raft]`, executes `pip-audit`, executes `tests/test_raft_backend.py`, then executes `tests/` with a JUnit output. It requires the Raft cases collected in the full-suite JUnit to have zero skips, failures or errors.

The workflow then executes `python -m build`, `python -m twine check --strict dist/*`, and checks selected required modules are present in each wheel and sdist. It uploads the distributions as a GitHub Actions artifact before the downstream PyPI publish job.

These are **workflow-defined gates**. This file does not claim to have independently retrieved the release run's complete logs, job IDs, artifact digests or PyPI file bytes. The required-module check is not exhaustive source equivalence. Successful publish does not establish version-specific Zenodo archival, a Sigstore attestation or source-to-binary reproducibility.

## Exact evidence still to capture

| Evidence | Required recording | Status |
|---|---|---|
| Publish run identity | Workflow run URL/ID, event, ref and `head_sha` equal to the tag SHA | Pending capture |
| Job results | Build and publish job conclusions, exact attempt, runner and Python version | Pending capture |
| Test outcomes | Full-suite JUnit PASS/FAIL/SKIP plus Raft subset counts; never sum overlapping runs | Pending capture |
| Release file inventory | PyPI JSON `urls[]`: exact filenames, upload times, sizes, hashes and package metadata | Pending capture |
| Wheel and sdist bytes | SHA-256 of independently downloaded files matching published hashes | Pending capture |
| Source equivalence | Compare normalized Python sources, schemas, templates, version, licenses and package metadata against tagged source; enumerate generated/excluded files | Pending analysis |
| Installation | Install pinned distribution into clean supported environments, report imports and smoke-test results | Pending execution |
| Attestations | Note actual PyPI provenance/Sigstore information, if any; do not infer | Pending observation |
| DOI | Look for an independently published version-specific Zenodo record; concept DOI is not sufficient | Pending observation |

## Suggested read-only collection

These commands **collect evidence; they do not publish**. Run where PyPI and GitHub are reachable:

```bash
python -m pip download --no-deps --only-binary=:all: --dest artifacts "tenir-governance==5.7.0"
python -m pip download --no-deps --no-binary=:all: --no-build-isolation --dest artifacts "tenir-governance==5.7.0"
python -m pip hash artifacts/*
python -m pip show tenir-governance
```

The sdist download may require local build prerequisites; if it fails, report that failure and use the exact PyPI download URL from verified metadata rather than assuming the wheel and sdist are equivalent. Run `pip show` only after installing the same pinned version in a controlled environment; it is **not** itself proof of the downloaded archive's identity. Record the independent SHA-256 values and compare with PyPI-provided checksums before declaring provenance verified.

For source comparison, treat archive metadata and generated build files separately from source-content equality. Match each included application source against the tagged tree; list omissions/extras explicitly. No hash of one artifact alone establishes correspondence to a git tree or identity with a different archive.

## Documentary release gate

Until this table is populated with immutable references and inspected bytes, use **verified publication, unverified binary correspondence**. Avoid labeling CI green as independent review, formal assurance, end-to-end integrity or production certification. Findings about v5.7.0 should cite the tag, not later `main` documentation patches.
