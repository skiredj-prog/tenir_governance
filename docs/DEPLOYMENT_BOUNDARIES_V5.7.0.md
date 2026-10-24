# TENIR-Gov v5.7.0 — Deployment and guarantee boundaries

**Status:** post-release operational guidance; **not** an attestation or replacement for the tagged source.  
**Release SHA:** `47ef67e3efc5f3008896a7d88bebf929fa0a98f0`  
**Scope:** single-host; FM-20 open; multi-host production unsupported.

## Separate operations

**Installation:** `python -m pip install "tenir-governance==5.7.0"` obtains the package; it does **not** enable a production-safe runtime or prove conformance.

**Verification:** the `tenir_kernel.deployment_manifest.verify_deployment_manifest(path)` function validates the deployment manifest against the packaged JSON Schema and verifies declared artifact SHA-256 digests. Verification is about a declared set of local artifacts. It does not verify the operator's intentions, provider effects, external trust, or regulatory compliance.

**Production startup:** choose `TENIR_ENV=production`, `HOST_SCOPE=single-host`, and `TENIR_DEPLOYMENT_MANIFEST=/absolute/path/to/deployment.manifest.json`. The API/runtime startup must invoke the manifest guard in its supported entry point; simply exporting variables or validating a file does not prove every custom integration enforces the guard. Configure an explicit durable operation registry as well as the budget backend; in-memory registry state cannot preserve idempotency over restart.

## Manifest contract

Source of truth: `tenir_kernel/schemas/deployment.manifest` and `tenir_kernel/deployment_manifest.py`.

The schema requires: `manifest_version` (`"1"`), `deployment_id`, `release` (`tenir-governance`, version), `runtime_contract` (`TENIR-2.0`, schema `1.2`), `deployment` (environment and host scope), `mode`, `governance_controls` (I2 and I5 true), `budget_store` (`sqlite_wal`, database), `raft` (configuration and `production_allowed=false`), and a **nonempty** `artifacts` list. Each relative artifact path must exist under the manifest directory and match its supplied 64-digit SHA-256 value.

A production v5.7.0 manifest must select `single-host`, `raft.enabled=false`, `raft.serialization=not_used`, and `raft.production_allowed=false`. Setting `raft.enabled=true` in production or `host_scope=multi-host` is rejected for v5.7. This is a *configuration refusal*, not a security proof of the remaining deployment.

## Example outline (not deployable as-is)

The following is **deliberately incomplete**, to avoid an invented artifact digest, directory or approved production mode:

```json
{
  "manifest_version": "1",
  "deployment_id": "REPLACE_WITH_REAL_DEPLOYMENT_ID",
  "release": {"package": "tenir-governance", "version": "5.7.0"},
  "runtime_contract": {"engine_spec_version": "TENIR-2.0", "schema_version": "1.2"},
  "deployment": {"environment": "production", "host_scope": "single-host"},
  "mode": "ENFORCE",
  "governance_controls": {"i2_payload_binding": true, "i5_conflict_hold": true},
  "budget_store": {"backend": "sqlite_wal", "database": "REPLACE_WITH_DURABLE_LOCAL_PATH"},
  "raft": {
    "enabled": false, "peer_authentication": false,
    "serialization": "not_used", "production_allowed": false
  },
  "artifacts": [
    {"path": "REPLACE_WITH_REAL_RELATIVE_FILENAME", "sha256": "REPLACE_WITH_ACTUAL_SHA256"}
  ]
}
```

Replace placeholders with inspected deployment data and compute every digest from the actual file. Check schema, file permissions, identity controls, storage recovery and restore procedures before production. Do not copy an illustrative example into production without engineering review.

## Guarantees and limitations by component

| Component | Supported statement | Explicit exclusion |
|---|---|---|
| Kernel scoring | Deterministic evaluation under configured inputs/policy | Not an empirically calibrated universal safety metric |
| Governor | Governs state transitions and bounded dispatch protocol | Does not guarantee a provider action can be undone |
| Operation registry | Durable idempotency only when configured with persistent storage | In-memory default loses state on restart |
| SQLite WAL budget | Local transactional accounting on one host | Not cross-host distributed consensus; not cross-store/provider atomicity |
| Raft backend | Development/test loopback regression characterization | FM-20 pickle open; not production multi-host, no C3 claim |
| EP-02 Profile S | Optional local commit authorization, disabled by default | Preflight or generic CAS receipt is not dispatch authority |
| HOLD / UNKNOWN | Records suspended or unresolved pathways | No automatic safe retry based solely on missing provider evidence |
| Audit ledger | Detectability of specified record alterations | Not truthfulness of inputs, correctness of decisions or regulatory certification |

## Evidence qualification

Only quote test PASS/FAIL/SKIP with the exact source SHA, workflow run ID, test selection, dependencies and platform. Raft targeted cases are included again in the full suite: do not double-count. Historic 96% coverage and latency figures are not measurements of the v5.7.0 published tag without an exact-tag reproduction.

This document explains the **declared** boundary. Before an institutional deployment, verify the precise SDK/API entry point, persistent registry configuration, manifest artifact list and production readiness in the environment used.
