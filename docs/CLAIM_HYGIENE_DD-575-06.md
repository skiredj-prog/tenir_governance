# Claim hygiene notes for DD-575-06 (v5.7.0 baseline)

Apply these constraints when editing README, SECURITY.md, deployment docs, and HITL materials. Do not introduce new guarantees.

## Mandatory non-claims

- A signature (OIDC, Sigstore, GPG, or in-toto) does not by itself prove runtime safety, provider effects, or multi-host fault tolerance.
- No exactly-once external provider effect is claimed. UNKNOWN remains unresolved; compensation is idempotent reconciliation, not a guarantee of single execution.
- FM-20 remains open. Production multi-host Raft and C3 are not claimed. `production_allowed` stays false for Raft.
- Historic 96 % statement coverage and earlier latency figures are not measurements of the v5.7.0 published tag. Any coverage number must name the exact SHA, package, omitted paths, and platform.
- CI success, PyPI publication, and deployment qualification are distinct. Green tests do not constitute independent certification or a production-readiness certificate.
- The ledger proves record integrity, not truthfulness of inputs or correctness of policy decisions.
- EP-02 Profile S is opt-in and disabled by default; preflight or generic CAS receipt alone does not authorize dispatch.

## Preferred wording patterns

- Prefer “bounded single-host profile”, “scoped test outcome”, “explicit exclusion”, “HOLD / UNKNOWN”.
- Avoid unqualified “secure”, “certified”, “exactly-once”, “fault-tolerant”, “96 % coverage”, or “production multi-host”.
- When citing tests, require exact SHA, workflow run, platform, and selected paths.

## Locations to review

- README.md (deployment boundary box, test-suite section, performance section)
- SECURITY.md
- docs/DEPLOYMENT_BOUNDARIES_V5.7.0.md (already strong; keep aligned)
- docs/POST_RELEASE_V5.7.0_DOCUMENTARY_STATUS.md
- Any HITL or impact-assessment text that could be read as expanding the published boundary

These notes do not themselves close DD-575-06; they define the acceptance criteria for the hygiene pass.
