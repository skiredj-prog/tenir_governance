# P0 decision record — T6/T7 native candidate and governor operation boundary

Date: 2026-10-25
Status: **P0 READ-ONLY INTEGRATION VERIFIED / EXECUTION COMMIT AUTHORITY HOLD**
Applies only to draft TENIR-Gov PR #65 and isolated REG Conformance PR #48.

## Verified scope

- Durable, single-host SQLite attempt qualification; signed Ed25519 attestation of attempt ID, logical effect identity, nonce, operation SHA-256 fingerprint, factual assertion, signer, TTL.
- UNKNOWN remains UNKNOWN after expiry or rejected verification. Signed EFFECT_CONFIRMED -> RESOLVED and signed NON_EXECUTION_CONFIRMED -> FAILED **in the attempt ledger only**.
- Replay invariants, terminal immutability, LEI uniqueness, operation-key uniqueness; cross-process conflicting qualification serializes, and a failed event write rolls back the attempted SQLite terminal transition.
- The read-only `AttemptOperationObservationBridge` binds to the actual persistent JSON `DurableOperationRegistry` and checks signed candidate evidence against the authoritative source operation. Corruption, missing identity, changed state, unsigned verifier, operation fingerprint mismatch and invalid linkage fail to `HOLD`.
- `inspect_governor_attempt` accepts a real `DualKernelGovernor` but **always returns `decision=HOLD`, `execution_authorized=false`**, and never calls the provider or performs budget settlement. This is intentional, not a stub for automatic dispatch.
- 47 scoped source tests passed on commit `49afb1a1d9dbb1ca3e74d946fd1ea4fab1101c8a`: [CI 40167328231](https://github.com/skiredj-prog/tenir_governance/actions/runs/40167328231). [Governance CI 40167328229](https://github.com/skiredj-prog/tenir_governance/actions/runs/40167328229) also passed.
- Independent REG candidate run [40167424102](https://github.com/skiredj-prog/reg-conformance/actions/runs/40167424102) passed with pinned source and machine-enforced non-promotion.

## Blocking architecture issues (must NOT be silently resolved by the observer)

| Boundary | Current evidence | Production-grade closure requirement |
| --- | --- | --- |
| JSON operation registry × SQLite attempt ledger | Two independent committed stores; no atomic transition | Define one authoritative transactional ledger or a reviewed durable prepare/commit/reconcile protocol with crash/restart invariants |
| Trust roots/revocation | Local configured Ed25519 allowlist/revocation and signed fixtures | Institutional issuer governance, independent root rotation/revocation freshness, independent clocks, anti-rollback evidence |
| Factual truth of a signed no-effect claim | Signer authenticity demonstrated; external factual correctness NOT proven | Provider / independent authority reconciliation and binding to an external durable effect receipt |
| Liveness | Timeout cannot terminalize UNKNOWN | Specified `tau_K`, escalation, expiry/revocation and adjudication interaction under partition |
| T8 | New attempts remain denied for the same LEI | Single-use policy- and evidence-qualified retry authorization, crash-safe consumption, quarantine and new attempt binding |
| Distributed execution | Only single-host SQLite plus persistent JSON registry | Independent multi-host consistency and partition tests, with explicit non-claims if not implemented |

## Decision

**ACCEPT** isolated P0 observation code and automated evidence as a reviewable draft.

**HOLD** any claim of native normative CT-R4-011/012 conformance, production dispatch authorization, T8 admission, REG+ promotion, publication, merger and packaging.

A green test result does not allow a silent transition from `EVIDENCE_RECORDED_NO_COMMIT_AUTHORITY` to operational `PASS`. Source and REG candidate require separate review and approval before merging.
