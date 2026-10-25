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

## P0 atomic single-store candidate (2026-10-25)

A separate experimental authority now exists at `tenir_governance.operations.attempt_atomic_candidate.AtomicAttemptCandidateAuthority`. In one SQLite WAL transaction it writes:

- A **candidate** source operation projection, uniquely identified by operation key and LEI.
- The one permitted attempt for that source, with a signed operation digest, attempt ID, LEI, nonce and qualification deadline.
- The signed evidence receipt and matching source/attempt terminal states, plus a durable event.

The transaction is serialized by `BEGIN IMMEDIATE`, with `synchronous=FULL` and foreign-key enforcement. SQLite triggers prevent identity mutation, terminal-state rewrites and event deletion/modification. The constructor rejects arbitrary verifier callbacks; callers must supply the configured Ed25519 verifier. Conflicting signed outcomes compete for one terminal transition; an exact repeat is idempotent. The tests exercise two-process races, injected rollback and **real child-process hard exit immediately before COMMIT** and immediately after successful COMMIT, followed by restart/replay.

**P0 scoped result:** [66 targeted tests PASS](https://github.com/skiredj-prog/tenir_governance/actions/runs/40168273609) on commit `105ad0755c9730f6c003d46fb98acd5198710a3d`. This proves atomicity of *candidate projection + attempt + evidence* within a single tested SQLite database, not atomicity of a source JSON operation with a second database, and not power-loss/institutional durability.

### Non-contamination constraints

`CandidateAtomicObservation.decision` is always `HOLD`; `execution_authorized=false`; `is_governor_operation_state=false`. The candidate source projection is a separate experimental model, **not** a replacement or mirror authoritative for `DurableOperationRegistry`. It cannot reserve, authorize, dispatch, compensate or grant retries. The production governor and its JSON registry are unchanged.

### Decision options for production work

**Option A — unify actual operation, attempt and budget authority in one transactional durable backend (preferred engineering direction, subject to design review).** Define an integration/migration plan which gives the governor one authoritative operation state and a transactionally bound attempt and evidence lifecycle, respecting the existing budget/consensus contract. Then test crash recovery at every boundary, dispatch claims, provider late effects and budget I8. The candidate is a specification/test fixture, not a deployable adapter.

**Option B — keep separate durable stores with an explicit prepare/reconcile coordinator.** This demands a proved recovery protocol with idempotent state transitions and safe uncertainty, particularly under crash, lost responses and quarantine. Merely acquiring a lock or checking both stores twice is inadequate.

Neither option is approved for execution by these tests. Native normative CT-R4-011/012, T8, production settlement, cross-host and published REG+ claims remain **HOLD**.

## Budget colocation and updated CI (2026-10-25)

The one-store candidate schema can also be created **in the same physical SQLite file** as `SQLiteConsensusBackend` without changing real reservation ownership. Tests explicitly demonstrate that even an authenticated no-effect attestation **does not call release or refund**, and an existing COMMITTED budget remains COMMITTED; any compensation requires a separate approved authority and transaction contract.

- [Source scoped CI 40168586084](https://github.com/skiredj-prog/tenir_governance/actions/runs/40168586084): **69/69 PASS**.
- [Independent REG candidate CI 40168660397](https://github.com/skiredj-prog/reg-conformance/actions/runs/40168660397): **PASS** on source pin `18d24dda78f0234550311ddf1bbcb6a0d94621b2`.
- Registry JSON is still not part of the SQLite transaction, budget APIs commit in separate transactions, and no external effect is authorized. Physical co-location is a feasibility result only.

The explicit migration acceptance gates are recorded in `docs/ADR_P0_67_UNIFIED_AUTHORITY.md`. Production P0 **remains HOLD** pending migration and full provider/budget crash verification.
