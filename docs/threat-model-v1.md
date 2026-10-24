# TENIR-Gov threat model v1 — v5.7.0 source line

**Review state:** bounded source-code assessment, not a certification. Publication status requires checking the actual GitHub tag/release and PyPI artifact; this file alone does not establish either. Independent Harv review is not asserted as completed.

## Covered controls

| Invariant / risk | Status | Implementation evidence | Limit |
|---|---|---|---|
| I2 / FM-02 — permit bound to authorized payload | Implemented in the inspected source line; evidence is scoped to referenced tests and an exact commit | `tenir_governance/operations/payload_binding.py`; governor adjudication/dispatch; `tests/test_composition.py` | Hash binding covers canonical JSON payloads. It does not replace provider-side authorization or protect a provider after dispatch. |
| I5 / FM-05 — contradictory failure and S3 safety evidence | Implemented in the inspected source line; evidence is scoped to referenced tests and an exact commit | `tenir_governance/composition/governor.py`; `tests/test_provider.py` | The HOLD path depends on the provider adapter exposing the explicit safety signal; unstructured provider claims are not inferred. |
| I8 / F-01 — late effect after release | Implemented in the inspected source line; evidence is scoped to referenced tests and an exact commit | `operations/raft_backend.py`, `operations/sqlite_backend.py`; `tests/test_raft_backend.py`, `tests/test_p0_sqlite_atomicity.py` | The code records a required compensation transaction; no external compensation is executed automatically. |
| F-02b — amount validation | Implemented in the inspected source line; evidence is scoped to referenced tests and an exact commit | `operations/budget.py`, `membrane/__init__.py`; `tests/test_p0_budget_validation.py` | Every deployment adapter must preserve the same validation boundary. |
| F-02c — atomic local durable accounting | Implemented for the SQLite backend in the inspected source line; verify exact-commit test evidence | `operations/sqlite_backend.py`; `tests/test_p0_sqlite_atomicity.py` | SQLite backend is single-host. Legacy JSON and Raft journal paths are not made atomic by this component and must not be represented as SQLite-backed. |

## Open gaps and release gates

| Risk | Status | Required closure evidence |
|---|---|---|
| FM-20 — Raft network serialization and peer trust | **Open** | PySyncObj command transport still uses pickle. `production_allowed` is fixed to `false` in the v5.7 manifest schema; v5.7 manifests reject `multi-host`; CLI, API and direct Raft startup apply the same fail-closed policy. Remediation target: **5.8.0**. Residual risk: development/test Raft still uses pickle and must not receive untrusted traffic. |
| F-02a — replicated determinism | Implemented in the inspected source line; evidence is scoped to referenced tests and an exact commit | Portable AST guard, logical clock, and replication convergence tests are present. |
| Whole-release proof | Pending | Exact-SHA local results are recorded in the separate report. Remote CI, the broader crash-recovery campaign, independent full-diff review, and Harv review remain open. |

## Deployment rule

FM-20 is an open risk, not a deferred or covered control:

```json
"FM-20": {
  "status": "open",
  "production_allowed": false,
  "remediation_target": "5.8.0",
  "mitigation": "Hard-disabled in v5.7.0 production manifest"
}
```

In the v5.7.0 release line, `HOST_SCOPE=multi-host` fails startup. Production startup requires a verified `deployment.manifest` matching the installed package version, `host_scope: single-host`, `raft.enabled: false`, and `raft.production_allowed: false`. Direct construction of the Raft backend also fails in production. Use SQLite WAL for the bounded single-host deployment profile. The 5.8.0 target is not evidence that FM-20 has been remediated; transport replacement and independent verification remain release gates.

The v5.7.0 product description is: **targets single-host deployments with explicitly bounded guarantees**. Harv's activity is an independent review, not a certification.

The planned 5.7.5 evidence report records observed tests, release identity, cryptographic file hashes, provenance, and scope limits. A matching hash shows that the report file has not changed; it does not prove that the system is safe, that a regulator has approved it, or that an agent/provider was uncompromised. A paid mono-host pilot may proceed at 5.7.5 only after the report and deployment prerequisites are accepted; it does not depend on multi-host 5.8.0.

## Evidence caveat

"Implemented in the inspected source line" is not a certification, a published-release claim or a verified production guarantee. The separate release-readiness report records what ran against the exact committed SHA. A report records bounded observations, not a guarantee of safety. Release readiness requires reproducible exact-SHA CI, relevant integration and recovery evidence, and explicit review; the existence or absence of an independent review must be reported accurately.
