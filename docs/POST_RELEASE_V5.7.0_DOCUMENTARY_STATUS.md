# TENIR-Gov v5.7.0 — Post-publication documentary status

**Date:** 2026-10-24  
**Nature:** Documentation-only corrective assessment; not a certification, signed attestation, or new release.  
**Release tag:** `v5.7.0`  
**Immutable source SHA:** `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`  
**GitHub release:** https://github.com/skiredj-prog/tenir_governance/releases/tag/v5.7.0  
**PyPI:** https://pypi.org/project/tenir-governance/5.7.0/  
**Zenodo:** `10.5281/zenodo.21264802` is the concept DOI for the all-versions series, **not** a version-specific v5.7.0 DOI.

## Historical records versus current status

The file `docs/release-v5.7.0-qualification.md` records a **pre-tag decision**. Its prepublication NO-GO is historical: the GitHub release and PyPI version have since been published. This file supersedes it **for publication status only**, not for implementation evidence or safety claims.

The archived `v5.7.0` tag is immutable for audit purposes. Later documentary corrections on `main` **do not modify the contents of the published source tag or the already-uploaded PyPI distributions**. Avoid describing those corrections as if they were part of the 5.7.0 artifact.

## Evidence hierarchy

1. **Identity and provenance:** Git tag SHA, GitHub release, PyPI file hashes and package metadata.
2. **Executable behavior:** source at tagged SHA, manifests, backend configuration, schema validation and default modes.
3. **Scoped observations:** CI results on exact SHA, JUnit PASS/FAIL/SKIP counts, platforms, replay/crash tests, integrity checks.
4. **Normative boundaries:** externally accepted requirements and conformance profiles; source implementation alone cannot establish external regulatory conformance.
5. **Exposition:** current guides and README; older roadmaps and ADRs describe their original decision date and baseline.

Neither CI green nor a cryptographic digest proves operational safety, provider-side effects, security of the host, or independent certification. `admissible_structural` is not equivalent to `effective_guarantee_demonstrated`.

## Current deployment boundaries

- **Single host only.** A verified production manifest, durable operation registry and bounded SQLite WAL budget configuration are needed for the claimed local durable profile.
- **FM-20 OPEN:** PySyncObj/pickle transport; Raft is restricted to declared development/test loopback use; production multi-host Raft and C3 are not claimed.
- **UNKNOWN is unresolved evidence, not FAILED.** HOLD is a governed stop/qualification; quarantine prevents specified new transitions, not retroactive cancellation of external effects.
- **Profile S:** EP-02 commit-authorization is opt-in and disabled by default. Preflight admissibility and generic CAS receipts alone do not authorize dispatch. See `docs/EP02_GOVERNOR_COMMIT_GATE.md`.
- **CAVE:** Control / Aim / Veto / Epistemics, per canonical `tenir_governance/nomenclature.py`.

## Claims awaiting evidence before institutional use

| Claim | Required exact evidence | Current documentary status |
|---|---|---|
| Distribution matches tagged source | Retrieve PyPI wheel/sdist bytes, hash them, inspect installed modules and compare canonical source | Not independently attested here |
| Test count and skip count for release | Release-to-PyPI workflow run on SHA `47ef67e3...` with JUnit outcomes by suite | Do not reuse historical 1,283 unqualified |
| Coverage | Exact-source `coverage.xml` or terminal report, options, package, platform | Do not call historical 96% a v5.7 measurement |
| I2, I5, I8 | Tagged-code test outcomes and specified adapters | Conditional, scoped to exercised paths |
| C1/C2/C3 | Explicit contract, environment and acceptance suite | No physical multi-host C3 assertion |
| External provider effects | Provider-specific idempotence and reconciliation evidence | No exactly-once external-effects claim |
| Performance | Hardware, dataset, concurrency, profile, samples and observed quantiles | Historical numbers are not v5.7 SLAs |
| Harv/independent review | Versioned signed report and reviewer scope | Not verified as complete |
| Zenodo v5.7 DOI | Published, accessible version-specific record | Not asserted by source metadata |

## Documentation closure gate

- Zero current release-status contradictions; historic material explicitly marked as such.
- Zero test/coverage/latency numbers represented as current without exact configuration and provenance.
- No promise extending from local tests to multi-host, provider side effects, formal proof or certification.
- Production examples must explicitly show persistent store and manifest requirements; demo installs must be labelled non-production.
- Every replacement statement must be checked against README, SECURITY, CHANGELOG, CITATION, Zenodo metadata, workflow configuration and current integration guide.
- Where post-release docs differ from released source artifacts, display their post-release date and scope; never rewrite the published SHA's history.
