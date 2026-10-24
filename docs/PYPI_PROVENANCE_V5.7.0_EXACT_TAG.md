# TENIR-Gov v5.7.0 — Exact-tag release provenance audit

**Audit date:** 2026-10-24  
**Status:** Release pipeline and downloaded GitHub Actions build artifact **verified**; independent retrieval of currently hosted PyPI files **not performed**. Documentation-only evidence supplement, not certification.  
**Release Git tag:** `v5.7.0`  
**Commit:** `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`  
**GitHub Release:** https://github.com/skiredj-prog/tenir_governance/releases/tag/v5.7.0  
**Release workflow:** https://github.com/skiredj-prog/tenir_governance/actions/runs/40119786756  
**Trigger:** push; workflow event timestamp 2026-10-24T18:14:26Z; final result **success**.

## Evidence of exact-SHA execution

| Job | Job ID | Result |
|---|---|---|
| Test and build distributions | `120948612615` | success |
| Publish to PyPI | `120950036948` | success |

From **build-job logs on the release SHA**, Python 3.12 / ubuntu-latest:

- `pip-audit --skip-editable`: “No known vulnerabilities found” **in the audited dependency selection at that time**, not proof of absence of all security vulnerabilities.
- Explicit Raft suite: **81 passed** in 147.98 seconds.
- Full `tests/` suite: **1,286 passed, 1 warning** in 182.39 seconds; the release's JUnit guard reported **81 Raft cases, 0 skipped**.
- Do **not** add Raft 81 to full-suite 1,286: they overlap.
- `python -m build`: wheel and sdist built successfully.
- `twine check --strict`: both distributions passed.
- Package module-presence check: passed.
- Release job did **not** record an exact-source statement coverage measurement; historical “96% coverage” does **not** apply automatically.
- These results do not demonstrate independent review, formal safety properties, deployment-wide security, physical multi-host C3, external exactly-once effects, or correctness of providers.

## GitHub Actions artifact — downloaded and hashed independently

Artifact ID: `12570084410`  
Artifact name: `python-package-distributions`  
ZIP size: **578,261 bytes**  
ZIP SHA-256: `80eb2a114a30a2ca966733ee69c34f18498780e7b9d32be313a5a6436b49d49c`

Archive contents:

| File | Bytes | SHA-256 |
|---|---:|---|
| `tenir_governance-5.7.0-py3-none-any.whl` | 241,062 | `47b59d7e590bb4dba149da4f49f603a4cf272e35044cdf7e37a207a0aa6a6e4e` |
| `tenir_governance-5.7.0.tar.gz` | 343,498 | `84b21b422e2ec9d1eb96ed128efd131e0fb5571761826519ef30dcf81d6c9cc4` |

All hashes above were recomputed from the downloaded GitHub Actions ZIP. They match the run's logged artifact checksum and both filenames' SHA-256 values in the PyPI publishing action's DSSE attestation output.

The **publishing job** downloaded the exact ZIP digest above, passed the strict distribution checks and returned **HTTP 200 OK** after uploading both files to `https://upload.pypi.org/legacy/`; the action logged `https://pypi.org/project/tenir-governance/5.7.0/` and completed with `success`. Digital publishing attestations were generated. This establishes the **observed successful upload**, but does not replace independent post-upload downloads directly from PyPI or full cryptographic verification of the attestations against their trust policy.

## Source equivalence checks

The inspected wheel has **73 entries** and sdist **143 entries**. The wheel has 66 `.py`/`.json` code/data entries; all 66 appear in the sdist with byte-identical contents (zero differences).

Five critical module byte sequences were compared using Git's blob SHA-1 object calculation to the exact blob SHAs returned by GitHub for the release tag:

| File | Git blob SHA-1 — equal for wheel, sdist, tagged source |
|---|---|
| `tenir_governance/operations/provider.py` | `36031730f955bdb6317a23af989c7b0c888d21db` |
| `tenir_governance/operations/registry.py` | `d20b4d186b8bcf4fbf51e63fddd0c7510d44c875` |
| `tenir_governance/composition/governor.py` | `a84509f4f4651f1c25106651a8c899594ab02d3a` |
| `tenir_kernel/deployment_manifest.py` | `9ef9fd536eeaf427d16ac410cefd8253762b4172` |
| `tenir_governance/operations/sqlite_backend.py` | `7becd7cd97ea9f6d847f404ee0a91c0ab20d6c68` |

**Extended post-audit check:** all 66 wheel Python/JSON files now have exact Git blob identity with tag `v5.7.0` and byte-equal matches in the source distribution. This does not claim byte-identity for packaging-generated metadata, the license, or non-JSON deployment manifest.

Wheel `METADATA`: package `tenir-governance`, version `5.7.0`, Python `>=3.10`, SPDX `Apache-2.0`, extras `api`, `r5`, `test`, `raft`, `ep02`. The DOI URL is clearly labelled **all versions**.

## Outstanding independent checks

1. Retrieve the **live PyPI-hosted** wheel and sdist directly, record their own SHA-256 and compare them against the values above. A separate web retrieval observed stale PyPI index content and a 404 from the version JSON endpoint; this conflicts with the exact-SHA successful upload trace. Do not conclude that the version is currently unavailable without a live independent check that is not stale/cached.
2. For **all** distributable code/data files (not just the five sampled critical modules), compare Git blob IDs against the release tag and document generated metadata exclusions.
3. Verify each publishing attestation's signature, OIDC identity and provenance trust policy, not just its digest in job logs.
4. Run installation and functional smoke checks from a **fresh wheel-only environment**. Do not treat editable-install tests as equivalent to installed-wheel tests.
5. Leave FM-20 open and ensure single-host deployment and HITL claims retain their documented conditional scopes.

**Decision:** GO for documenting successful tag-bound build, test and publish actions; **conditional / not yet closed** for independent live PyPI artifact retrieval and complete package equivalence. Nothing here changes the published tag or package.
