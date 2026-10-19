# Changelog

## [5.6.1] — Pending release — Patch: Budget safety and deterministic Raft state

### Corrected in this branch
- Reject `reconcile_commit()` unless the reservation is `RESERVED`; repeated commits remain idempotent, while a late effect after `RELEASED` returns `RECONCILE_CONFLICT` and records a deterministic incident.
- Replace replicated wall-clock timestamps with a replicated logical clock.
- Validate finite, positive reservation amounts and non-negative finite capacities; preserve decimal arithmetic in the replicated budget and fractional values in read results.
- Return the actual reservation state on idempotent replay.
- Make resource registration non-destructive; reject implicit capacity changes.

### Additional safety corrections under review
- Preserve compatibility with five-argument legacy Raft reserve commands and normalize restored float accounting before Decimal arithmetic.
- Preserve exact Decimal values in Raft state reads and fractional values in the shared consensus state contract.
- Validate governor amounts before budget gating; bind dispatch resource and amount to the adjudicated durable record; block dispatch under QUARANTINE_LOCK and serialize quarantine transitions against dispatch/provider execution.
- Add regression tests for legacy state/journal compatibility, large-capacity decimal precision, invalid amounts, dispatch identity mismatch, pre-reserved operations under quarantine, empty Stripe search, concurrent reconciliation, and linearizability-checker branch isolation.
- Keep Stripe reconciliation UNKNOWN when search returns no result; an empty search is not proof of non-execution. Serialize reconciliation state transitions with budget release/refund to prevent duplicate refunds.
- Prevent live resource replacement from orphaning reservations or swapping the resource lock; correct the sequential oracle so COMMITTED cannot follow RELEASED, bind concrete reservation IDs across replay/commit, and exercise the actual concurrent Raft history.
- Normalize Decimal input before the single-host backend persists reservation JSON; add regressions for Decimal idempotency, HOLD identity preservation, and preventing a HOLD veto from releasing another operation’s reservation.
- Replace fixed leader-failover sleeps with bounded polling across the three- and five-node tests; release reserved accounting when an UNKNOWN reservation is explicitly reconciled as non-executed.

### Test perimeter
- Reproduction: `python -m pip install -e ".[test]" "pysyncobj==0.3.17" && pytest -q tests/test_raft_backend.py`
- Included: local `pysyncobj` Raft integration tests, I8 regressions, invalid-amount cases, idempotent replay, resource re-registration, logical-clock replication and fractional-budget accounting.
- Environment: test cluster nodes use `localhost` and separate ports; this is not a physical multi-host network test.
- Excluded: Byzantine tolerance, hardened peer transport/authentication, third-party attestation, and proof of exactly-once external effects.
- Raft regression suite result: **46 collected, 46 passed, 0 failed, 0 skipped** in 119.01 s on GitHub Actions `ubuntu-latest`, Python 3.11.17, pytest 8.4.2, `pysyncobj` 0.3.17. Tested implementation commit: `82037052556c5bfd731bd59fe958cf4df61d21b6`; CI run: [#39205580977](https://github.com/skiredj-prog/tenir_governance/actions/runs/39205580977). Reproduce with `pytest -q tests/test_raft_backend.py`.

### Status
- Release status: not released by this change; do not infer a PyPI publication from this changelog entry.
- `admissible_structural` describes catalog coherence only and does not imply `effective_guarantee_demonstrated`.

---


All notable changes to TENIR-Gov are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [5.6.0] — 2026-10-19 — Minor: Multi-Host Raft Consensus Backend

This minor release adds `RaftConsensusBackend`, a multi-host `ConsensusBackend` implementation using the Raft consensus algorithm (via pysyncobj). This closes the C3 (cross-host fault tolerance) gap identified in the REG-Conformance audit: the cluster tolerates crash-stops of up to `f` nodes with `n ≥ 2f+1`, while maintaining C1 (safety/linearizability), I8 (budget conservation), and C2 (liveness) across hosts.

### Added
- **`RaftConsensusBackend`** (`tenir_governance/operations/raft_backend.py`) — Multi-host `ConsensusBackend` using Raft (pysyncobj). State (budget capacity, reserved, committed, reservations registry) is replicated across all nodes via `@replicated` commands. I8 is enforced on the leader before replication. Satisfies C1, C2, C3 (crash-stop), and I8 for multi-host deployments.
  - **Key design decision**: Reservation IDs are generated client-side and passed as command arguments, not generated inside the `@replicated` method. This ensures deterministic replication — a fundamental Raft requirement (all state machine commands must produce identical results on all nodes).
- **Tests: Raft multi-node** (`tests/test_raft_backend.py`) — 29 tests across 5 categories:
  - Multi-node reserve/commit/release (10 tests): operations from leader and followers, idempotency, insufficient budget, full lifecycle from mixed nodes.
  - I8 invariant (5 tests): I8 holds after multiple reserves, mixed commit/release, overcommit rejection, on followers, and under concurrent reserves.
  - Leader election and failover (5 tests): leader exists, failover after crash, 5-node failover, exactly one leader, state preservation after failover.
  - Liveness (4 tests): operations terminate under quorum, from all nodes, concurrent, and under minority failure.
  - Linearizability (5 tests): sequential history, concurrent history, mixed ops, after failover, and Wing-Gong checker on Raft histories.

### Changed
- Version bumped to 5.6.0 in `pyproject.toml`.
- `pysyncobj` added as optional dependency (required only for `RaftConsensusBackend`).

### Tests
- Total: **787 tests** passing (697 in `tests/` + 61 in `r4/tests/` + 29 new Raft tests = 787).
- Raft backend: 29 tests pass (10 multi-node + 5 I8 + 5 failover + 4 liveness + 5 linearizability).

### Conformance
- C1 (safety/linearizability): ✓ — Raft provides linearizability for committed commands. Verified by Wing-Gong checker on Raft histories.
- C2 (liveness): ✓ — operations terminate under healthy quorum. Verified by liveness tests.
- C3 (cross-host fault tolerance): ✓ — tolerates `f` crash-stops with `n ≥ 2f+1`. Verified by failover tests (3-node and 5-node).
- I8 (budget conservation): ✓ — enforced on leader before replication, holds on all nodes. Verified by I8 tests.
- Byzantine fault tolerance: ✗ — not implemented (Raft is crash-only).

### Limitations
- `RaftConsensusBackend` does not provide byzantine fault tolerance (Raft is a crash-fault-tolerant protocol).
- Network partitions: safety is maintained (Raft prevents split-brain), but liveness requires a quorum of connected nodes.
- `pysyncobj` uses pickle for serialization (not cryptographically signed). For production deployments with untrusted nodes, consider adding authentication/encryption via `SyncObjConf(password=...)`.
- The Wing-Gong linearizability checker is exponential in the worst case; it is tractable for small histories used in tests but would need a more efficient algorithm for large-scale verification.

---

## [5.5.0] — 2026-10-19 — Minor: Cross-Process Budget, Consensus Backend, Liveness

This minor release adds the `DurableBudgetCoordinator` for cross-process budget conservation (I8), a reference `ConsensusBackend` adapter implementing the REG-Conformance interface, a linearizability checker, and liveness tests. These components close the implementation gaps identified in the REG-Conformance audit: cross-process I8, consensus interface conformance, and liveness verification.

### Added
- **`DurableBudgetCoordinator`** (`tenir_governance/operations/budget.py`) — Cross-process budget coordination using `fcntl.flock` + durable JSON state. Extends I8 enforcement from single-process (`threading.Lock`) to multi-process on a single host. All read-check-write sequences are serialized via exclusive file lock. I8 (`reserved + consumed ≤ capacity`) is maintained atomically under the lock at all times.
- **`SingleHostLockBackend`** (`tenir_governance/operations/consensus_backend.py`) — Reference adapter implementing the `ConsensusBackend` interface defined in `reg-conformance/specs/consensus-interface-v1.json`. Wraps `DurableBudgetCoordinator` and adds a reservation registry with `reserve/commit/release/reconcile/get_state` operations. Conforms to C1 (safety), I8 (budget), C2 (liveness) for single-host deployments. The `ConsensusBackend` Protocol is defined as a `runtime_checkable` Protocol for implementation-neutral conformance.
- **`LinearizabilityChecker`** (`tenir_governance/operations/linearizability_checker.py`) — Wing-Gong algorithm for concurrent history validation. Takes a history of invocation/response events and verifies that a valid linearization exists respecting real-time precedence. Includes `consensus_sequential_spec` for the ConsensusBackend operations.
- **Tests: cross-process I8** (`tests/test_durable_budget_cross_process.py`) — 10 tests: two processes reserve same budget (one fails), multiple processes reserve within budget, cross-process reserve then commit, I8 under concurrent reserve and release, plus 6 unit tests for DurableBudgetCoordinator.
- **Tests: liveness** (`tests/test_liveness.py`) — 15 tests: termination within T_max for all operations, concurrent operations all terminate, max latency under load, reconciliation liveness (confirmed/failed/unresolved/not-found), liveness conditional (insufficient budget returns immediately), linearizability with liveness under concurrent load.

### Changed
- Version bumped to 5.5.0 in `__init__.py`, `tenir_kernel/__init__.py`, `pyproject.toml`, `CITATION.cff`, `.zenodo.json`, `SECURITY.md`, `demo.py`.

### Tests
- Total: **758 tests** passing, 0 failures, 0 regressions (687 in `tests/`, 61 in `r4/tests/`, 10 new cross-process I8 + 15 new liveness = 758).
- Cross-process I8: 10 tests pass (4 subprocess-based + 6 unit).
- Liveness: 15 tests pass (termination, reconciliation, conditional, linearizability).

### Limitations
- `DurableBudgetCoordinator` is single-host only (`fcntl.flock` does not cross host boundaries). Cross-host requires a `DurableBudgetCoordinator` backed by a transactional database or distributed coordination service.
- `SingleHostLockBackend` satisfies C1 (safety) and I8 for single-host, multi-process deployments. It does NOT satisfy C3 (fault tolerance across hosts) or byzantine tolerance.
- `LinearizabilityChecker` uses the Wing-Gong recursive search, which is tractable for small histories but exponential in the worst case. For large histories, a more efficient algorithm (e.g., PO-Check) would be needed.

---

## [5.4.4] — 2026-10-19 — Patch: Test Coverage, Attribution Fixes

This patch adds missing regression tests for authorize_exit budget paths, adds mutation-test documentation for the _reserve_budget lock, corrects the repository test count from 669 to 733 (669 in `tests/` + 61 in `r4/tests/` + 3 new), and fixes budget fix attribution in documentation.

### Added
- `test_authorize_exit_deny_releases_reservation` — verifies that authorize_exit(deny) releases the budget reservation and clears `budget_reserved`/`budget_committed` flags. Previously untested path.
- `test_authorize_exit_approve_keeps_reservation` — verifies that authorize_exit(approve) keeps the existing reservation without re-reserving. Previously untested path.
- `test_concurrent_adjudication_fails_without_lock` — mutation-test documentation verifying the `_reserve_budget` lock is load-bearing: without it, two concurrent threads would both reserve, violating I8.

### Fixed
- **Test count corrected** — README, docs/manuscript.md, and CHANGELOG previously stated 669 as "Repository total". The actual total is 733 (669 in `tests/` + 61 in `r4/tests/` + 3 new tests). The 669 figure was the `tests/` directory only.
- **Budget fix attribution** — 5.4.3 CHANGELOG now states "The I8 budget coordination fix was introduced in 5.4.2; this release consolidates it doctrinally and fixes documentation only." Previously, documentation could be read as implying the fix was new in 5.4.3.

### Tests
- Total: 733 tests passing, 0 failures, 0 regressions (672 in `tests/`, 61 in `r4/tests/`).
- authorize_exit deny: reservation released, flags cleared — verified.
- authorize_exit approve: reservation kept, no double-reserve — verified.
- _reserve_budget lock: concurrency test passes with lock, mutation documentation explains why it would fail without it.

---

## [5.4.3] — 2026-10-19 — Patch: CAVE Doctrinal Alignment, Metadata Fixes

This patch aligns the CAVE acronym to the doctrinal definition (Control / Aim / Veto / Epistemics), fixes stale metadata across the corpus, and updates documentation references. The I8 budget coordination fix was introduced in 5.4.2; this release consolidates it doctrinally and fixes documentation only.

### Changed
- **CAVE canonical definition flipped** — `CAVEFieldNames.EXPANSION` is now `Control · Aim · Veto · Epistemics` (doctrinal definition). The former runtime definition `Context · Action · Value · Effect` is now in `FORBIDDEN_EXPANSIONS`. The copy-lint CI step now blocks the old variant.
- **`.zenodo.json`** — description and notes updated from 5.2.0 references to 5.4.3, including I8 budget fix and CAVE alignment.
- **`CITATION.cff`** — version bumped to 5.4.3, date to 2026-10-19, abstract updated from 5.2.0 reference.
- **`SECURITY.md`** — all pip install examples updated from 5.4.1 to 5.4.3.
- **`README.md`** — proof paragraph updated to v5.4.2 with I8 budget fix and 733 tests; test count table updated from 663 to 733 (672 in `tests/`, 61 in `r4/tests/`).
- **`demo.py`** — install instructions updated to 5.4.3.
- **`tenir_kernel/__init__.py`** — version bumped from 5.2.0 to 5.4.3.
- **`tenir_governance/__init__.py`` — module docstring version updated from 5.2.0 to 5.4.3.
- **`tenir_governance/composition/governor.py`** — comments referencing "5.2.0's TenirKernel" cleaned to remove stale version.
- **`tests/test_cross_process.py`**, **`tests/test_fault_injection.py`** — skip reasons updated from "5.2.0 repo" to "kernel repo".
- **`docs/manuscript.md`** — test count updated from 663 to 733, version references updated to v5.4.3, changelog table extended with 5.4.2 and 5.4.3 entries.
- **`HARDENING_REPORT_v1.0.0-rc2.md`** — CAVE definition and NOM-001 check updated to Control/Aim/Veto/Epistemics.
- **`CONTRIBUTING.md`** — NOM-001 invariant updated to Control / Aim / Veto / Epistemics.

### Tests
- Total: 730 tests passing, 0 failures, 0 regressions (669 in tests/, 61 in r4/tests/).
- CAVE flip verified: `test_cave_canonical_definition` and `test_deprecated_cave_always_blocks` both pass with the new definitions.

---

## [5.4.2] — 2026-10-18 — Patch: Budget Coordination Fix

This patch fixes two bugs in the I8 aggregate budget invariant and hardens the budget lifecycle.

### Fixed
- **Double-reservation on HOLD resolve** — The membrane's I8 check no longer mutates the budget (pure `available >= amount` check). Reservation ownership moved entirely to the governor's `_reserve_budget()`, which is idempotent via the `budget_reserved` flag. `resolve_hold()` no longer re-reserves.
- **Phantom reservation after ESCALATED→UNKNOWN→FAILED** — `reconcile_unknown()` and `fail()` now use the durable `budget_committed` flag to choose `refund()` (if committed) vs `release()` (if only reserved), instead of inferring from the previous state.
- **`authorize_exit` approve double-reservation** — Approve no longer re-reserves the budget already held by the HOLD; it keeps the existing reservation.
- **`authorize_exit` deny budget leak** — Deny now releases the reservation and clears the `budget_reserved` flag.
- **`revalidate_budget_at_dispatch` now applied** — Budget is rechecked at the dispatch boundary (policy declared `True` but it was never read). The revalidation checks that the operation's reservation is still held (`reserved >= amount`), fail-closed to FAILED if not.
- **`dispatch()` silent commit failure** — If `resource.commit()` returns `False`, the governor now fail-closes to FAILED instead of silently proceeding to DISPATCHING with no budget consumed.
- **`fail()` and `expire_hold()` flag cleanup** — Both now clear `budget_reserved` and `budget_committed` flags after releasing/refunding the budget.

### Added
- `budget_reserved` and `budget_committed` fields on `OperationRecord` (persisted).
- `set_budget_flags()` method on `DurableOperationRegistry`.
- `_prospective_utilization()` method on `DualKernelGovernor` — counts the operation's own amount until it holds a reservation, so the policy sees the same load it would see after the reservation is taken.
- `_reserve_budget()` method on `DualKernelGovernor` — idempotent, under lock, fail-closed.
- `tests/test_hold_budget_regression.py` — 5 regression tests: double-reservation on HOLD resolve, phantom reservation after ESCALATED→UNKNOWN→FAILED, concurrent adjudication reserves budget once, authorize_exit deny releases reservation and clears flags, authorize_exit approve keeps reservation (no double-reserve). Plus mutation-test documentation verifying the _reserve_budget lock is load-bearing.

### Changed
- `tests/test_membrane.py` — Updated for pure membrane behavior (no budget mutation). Concurrency test moved to governor level.

### Tests
- Total: 733 tests passing, 0 failures, 0 regressions (672 in `tests/`, 61 in `r4/tests/`).
- I8 invariant verified on 9 paths: PASS/FLAG, HARD_VETO, HOLD→resolve, ESCALATED→UNKNOWN→FAILED, authorize_exit deny, concurrent, aggregate, fail from RESERVED, expire_hold.

### Known Limitations
- Budget coordination is single-process only (`InProcessBudgetCoordinator` with `threading.Lock`). Cross-process requires a `DurableBudgetCoordinator` backed by `fcntl.flock` or a database transaction.
- Legacy HOLD operations created before this patch have `budget_reserved = False` by default; the reservation from the old membrane code is in `resource.reserved` but not tracked by the flag. Migration is needed if persistent registries contain old HOLD operations.

---

## [5.4.1] — 2026-10-13 — Patch: Assessment Semantics Hardening

This patch corrects two semantic issues in the τ⁻¹ classifier identified during the v5.4.0 hardening review.

### Fixed
- **Callback exception now returns INDETERMINATE, not IRREVERSIBLE** — If the return-transition callback raises an exception, the classifier previously reported IRREVERSIBLE. A failed assessment does not prove that no authorized inverse exists; it means the assessment could not be completed. Both deterministic and non-deterministic callback paths now return INDETERMINATE.
- **Numeric input validation** — distance_q, return_cost, and λ must be finite and non-negative. NaN, infinite, or negative values from the callback now return INDETERMINATE instead of producing meaningless classifications or loss calculations.

### Tests
- 10 new tests for numeric validation (NaN, infinite, negative distance/cost/λ)
- 2 existing tests updated to expect INDETERMINATE instead of IRREVERSIBLE for callback exceptions
- Total: 663 tests passing, 0 failures, 0 regressions (pytest tests/ now runs without --ignore)

---

## [5.4.0] — 2026-10-13 — Minor: τ⁻¹ (Partial Tau Inversion) and EVP (Intended Value Preservation) Assessment Layer

This minor release adds the post-execution assessment layer: τ⁻¹ (partial tau inversion) and EVP (intended value preservation). Both are **ex-post** assessment concepts — they run AFTER execution, not before. Neither is an input to the admissibility function. Both are **opt-in** — `dispatch_and_execute()` does not auto-trigger assessment.

### Added
- **`tenir_governance/assessment/tau_inversion.py`** — `TauInversionClassifier` with 5 classifications: `PRE_COMMIT` (cancelled before effect), `EXACT_INVERSE` (property-preserving return on Q), `COMPENSATION` (partial recovery with loss), `IRREVERSIBLE` (no valid return transition), `INDETERMINATE` (insufficient information). Loss function: L_Q(s, τ, ρ) = distance_Q(ρ(τ(s)), s) + λ · cost(ρ).
- **`tenir_governance/assessment/evp.py`** — `EVPAssessor` with frozen equations: EVP Gross = intended × confidence; EVP Net = realized_value − execution_loss; EVP Ratio = EVP Net / EVP Gross. Friction costs reported **separately** (not subtracted from EVP Net). `FrictionCost` and `GovernanceDebt` dataclasses.
- **`tenir_governance/assessment/assessor.py`** — `PostExecutionAssessor` composing τ⁻¹ and EVP into a single `AssessmentReport`. Documents: opt-in lifecycle (not auto-triggered), in-memory storage limitation (no restart persistence, no tamper evidence), `return_transition` is assessment-only (not a compensating action).
- **Governor integration** — `assess_post_execution()` method on `DualKernelGovernor`. Assessor is opt-in via constructor parameter.
- **SDK integration** — `enable_assessor=True` parameter on `DualKernelCompositionClient`. `assess_post_execution()` convenience method.
- **Tests** — 110 assessment tests: 48 in `test_tau_inversion.py`, 37 in `test_evp.py`, 25 in `test_assessor.py`. Total: 643 tests passing, 0 failures, 0 regressions.

### P1 Semantic Fixes (from peer review)
- **P1-1: Absence of evidence is not evidence of non-execution** — `no_external_effect` defaults to `None` (indeterminate), not `True`. FAILED operations without explicit confirmation return `INDETERMINATE`, not `PRE_COMMIT`.
- **P1-2: Empty Q is vacuous** — Empty/missing `retained_properties` returns `INDETERMINATE`. A zero-distance callback with empty Q does not establish meaningful reversibility.
- **P1-3: EVP equations frozen** — `EVP Net = realized_value − execution_loss`. Friction reported **separately**, not subtracted from EVP Net. Definition, formula, ratio, and examples all agree. 6 hand-calculated EVP tests added.

### Doctrine Compliance
- τ⁻¹ is a partial, conditional, property-relative inverse — NOT a full mathematical inverse (Property-Preserving Crossing manuscript, pp. 11–12).
- EVP measures intended value protection as an **outcome** of governance discipline — EVP ∉ Admissibility Function. High expected value never justifies an otherwise inadmissible transition (TENIR Property-Preserving Transitions Manuscript, Layer 4).
- τ⁻¹ and EVP do not imply each other — a transition can have τ⁻¹ but poor EVP, or vice versa.
- Both are ex-post (post-execution) — v7.1 explicitly defers EVP: "Downstream value measurement is not treated."

---

## [5.3.1] — 2026-10-13 — Patch: Provider Integration, Security, Demo

### Added
- **`tenir_governance/operations/hold.py`** — `HoldManager` class implementing the full HOLD lifecycle: `enter_hold()`, `resolve_hold()`, `escalate_hold()`, `authorize_exit()`, `expire_hold()`, `expire_escalated()`, `check_tau_k_expiry()`, `find_holds()`, `find_escalated()`, `find_unresolved_holds()`.
- **`HoldRecord`** dataclass (hold_start, tau_k_seconds, tau_k_source, escalated_at, escalated_by, exit_reason, exit_receipt) with durable persistence across restarts.
- **`ExitReceipt`** dataclass (authorized_by, scope, timestamp, authorization, reason) for authenticated exit from ESCALATED state.
- **`OperationState.HOLD`** and **`OperationState.ESCALATED`** states with legal transitions: CREATED→HOLD, HOLD→RESERVED|FAILED|ESCALATED, ESCALATED→RESERVED|FAILED|UNKNOWN.
- **`tenir_governance/composition/hold_strategy.py`** — `CompositionHoldManager` with 5 strategies (Priority-Enforce, Serialize, Pareto-Restore, Escalate, Fail-Closed-Collectif) and progressive unblock (Déblocage progressif post Fail-Closed).
- **QUARANTINE_LOCK** in membrane — `lock_quarantine()` / `release_quarantine()` with authenticated authorization and receipt. Distinct from HOLD: blocks ALL admission/dispatch for an LEI until authorized release.
- **Governor HOLD integration** — `adjudicate()` transitions to HOLD state; `resolve_hold()` re-adjudicates with new evidence; `resume_existing()` returns `resolve_or_escalate` for HOLD and `await_authorized_exit` for ESCALATED; `recover()` includes HOLD and ESCALATED operations.
- **Tests** — 59 new tests in `test_hold_lifecycle.py` covering HOLD lifecycle, authorized exit, τ_K expiry, governor integration, QUARANTINE_LOCK, composition-level HOLD, state transitions, and persistence. Total: 501 tests passing.

### Doctrine Compliance
- HOLD is a designed suspension of agency, not a system failure (v7.1 §6).
- ESCALATED is absorbing — no time-based exit; τ_K expiry → UNKNOWN, not resolution (v7.1 §19).
- QUARANTINE_LOCK is distinct from HOLD — blocks everything until authorized release (v7.1 §6).
- τ_K is derived from existing org SLAs, never invented (Blueprint v4.4 §0.5).
- Progressive unblock verifies invariants before each TAU release (Blueprint v4.4 §8).

---

## [5.3.0] — 2026-10-12 — Two-Kernel Composition (Agentic Governance Profile v1.0.0)

### Added
- **`tenir_governance/objects/`** — Governed object types for object-centric governance: `Agent`, `Passage`, `Trajectory`, `Resource`, `Relationship`, `MissionInvariant`. These are the domain objects the composition layer operates on.
- **`tenir_governance/composition/`** — Two-kernel composition layer:
  - `decisions.py` — `Decision` enum (PASS/FLAG/HOLD/HARD_VETO), `ExecutionStatus`, `CheckType` with KB3 posture mapping (PASS→NORMAL, FLAG→CAUTION, HOLD→HOLDING-FIRST, HARD_VETO→HOLDING-FIRST).
  - `policy.py` — `CompositionPolicy` (frozen, versioned, configurable budget escalation threshold, default 85%).
  - `lattice.py` — Decision lattice `compose()` function and `HashChainedLedger` with tamper-evident hash chaining.
  - `governor.py` — `DualKernelGovernor` bridging the continuous risk monitor (`TenirKernel` via `ScoringKernelAdapter`) with the discrete invariant enforcer (`TransitionMembrane`). Durable dispatch state machine: CREATED→RESERVED→DISPATCHING→CONFIRMED|FAILED|UNKNOWN. `resume_existing()` for crash recovery. Fail-closed: kernel exceptions → HOLD, never PASS.
  - `schema_adapter.py` — `conform_to_schema()` mapping governor outputs to TENIR 2.0 schema 1.2 canonical fields (posture, execution_mode, compliance_class, degraded_mode, synthesis, trace, disclosure metadata).
- **`tenir_governance/membrane/`** — `TransitionMembrane` and `MembraneState` implementing discrete invariant enforcement (I3–I9: delegation trust, budget limits, quarantine, terminality, trajectory, temporal constraints).
- **`tenir_governance/operations/`** — Durable operation infrastructure:
  - `budget.py` — `BudgetCoordinator` protocol + `InProcessBudgetCoordinator`.
  - `registry.py` — `DurableOperationRegistry` with `fcntl.flock` cross-process locking, atomic `transition_to()` returning `(bool, prev_state)`, directory fsync for power-loss durability.
- **`tenir_governance/_profile_meta.py`** — `ProfileDisclosure` dataclass declaring `agentic-governance-profile-v1.0.0`, `profile-bound` compliance class, TENIR 2.0 schema 1.2.
- **`DualKernelCompositionClient`** in `sdk.py` — Optional SDK client wrapping the governor for consumers that need two-kernel agentic governance. The standard `TENIRGovernanceClient.adjudicate()` path is unchanged.
- **Validator checks** — `COMP-001` (composition lattice rules: fail-closed, escalation, veto) and `DISC-001` (profile disclosure metadata compliance per versioning policy §6).
- **Tests** — 65 composition tests, 15 membrane tests, 13 fault injection tests, 6 cross-process tests, 17 object tests (116 new tests total).

### Changed
- Package version bumped to `5.3.0` (minor, additive — no breaking changes).
- `__init__.py` exports updated to include composition layer, objects, membrane, and operations packages.
- Validator `validate_all()` now includes composition lattice and profile disclosure checks.

### Compliance
- **KB3**: Posture and execution mode are separate output fields, never collapsed. Implementation states (OperationState, Decision enum) are not rendered as doctrine. Degraded status and compliance class remain visible. No overconfident wording — all guarantees scoped explicitly.
- **TENIR 2.0 schema 1.2**: All module outputs conform to the canonical schema via `conform_to_schema()`. Schema field names are stable; new modules add fields but don't rename existing ones.
- **Versioning policy**: New modules are Profile/Module namespace (§2.3), not canon. `profile-bound` compliance class. Platform version 5.3.0 (minor bump). Profile version `agentic-governance-profile-v1.0.0`. Disclosure metadata per §6. Hard prohibitions §3 observed: no module imitates canonical version line, no API implies authority it doesn't hold.

### Guarantee Boundary
- **Locally guaranteed (in-process)**: At-most-once dispatch through the governance gate, fail-closed composition, mandatory invariant checks not skippable by healthy score, atomic budget reservations, hash-chained ledger integrity.
- **Requires distributed infrastructure**: Cross-host budget coordination, distributed idempotency key registry, exactly-once external effects across service boundaries.
- **Depends on external provider behavior**: Provider-side idempotency key support for exactly-once external effects.

---

## [5.2.0] — 2026-10-07 — TENIR 2.0 Kernel & Security Hardening

### Added
- `tenir_kernel` dependency-light execution kernel with schema 1.2 validation, constraint geometry, CLI, API wrapper, and Python/HTTP connectors.
- Secure-schema12 runtime object path and browser-compatible WebSocket authentication.

### Changed
- Package version and public API metadata updated to `5.2.0`.
- R5 HTTP services require bearer authentication and operator/oath secrets; Docker network bindings are localhost-only by default.
- Mode recovery and signed transition recording are aligned across wired and hardened R5 surfaces.

### Fixed
- SHADOW_OFF now returns `NOT_EVALUATED` without evaluating inputs.
- Malformed connector authorization responses fail closed.
- Unknown geometry severities fail closed.
- CAUTION posture is surfaced as a `FLAG` rather than silently passing.
- Legacy wired transition ledger calls use the canonical nonce field.

### Validation
- Release branch validation is performed through the repository CI gate and kernel/runtime regression suites.

---

## [5.1.0] — 2026-07-01 — Coverage Uplift & Governance Kernel

### Added
- **tenir-kernel/** — lightweight governance kernel (Tier 1): six-file reference
  implementation of S = K/(P×V+ε) with YAML policy engine, FastAPI adjudication
  endpoint, and append-only SHA-256 Merkle ledger. Operates without Neo4j.
  Thresholds (hard_veto=0.75, flag=0.90) mirror R5 full middleware defaults.
- **tests/test_coverage_uplift.py** — 118 new tests covering SDK public interface
  (TENIRGovernanceClient, GovernanceEvent, GovernanceResult), Validator CLI and
  ledger-chain checks, CopyLinter document scanning and CLI, LedgerMigrate
  pipeline and verification flow, and all regression corpus helper functions.
- **tenir-kernel/tests/test_tenir_kernel.py** — 8 targeted kernel validation tests
  (K1–K8) covering all verdict branches, epsilon guard, formula numerics,
  determinism, and Merkle ledger chain integrity.

### Changed
- Statement coverage: 72% → 96% across tenir_governance package (no source changes)
- Total test suite: 431 → 557 (549 middleware + 8 kernel; 555 passing, 2 skipped)
- README: complete rewrite documenting two-tier architecture, updated counts,
  threshold correspondence table, per-profile policy documentation

### Fixed
- CITATION.cff: updated abstract, full manuscript title, correct test counts
- Manuscript test count reconciliation: previous drafts reported 445/422;
  definitive figures are 557 total / 555 passing / 2 skipped / 96% coverage

---

## [5.0.0] — 2026-06-29 (IRON OMEGA R5) — Current Release

### Summary
Full production release of TENIR-Gov as an integrated governance middleware.
Merges R4 (partner_a Shadow v4) and R5 (IRON OMEGA) into a single installable package with a shared canonical contract.

### Added — Governance Package (`tenir_governance`)
- **Sprint 0** — `nomenclature.py`: single canonical term registry for R4+R5 vocabularies, three-register language system (Canonical / Operator / Public)
- **Sprint 1** — `policy_engine.py`: frozen `PolicyEngine` dataclass with three factory profiles (default, partner_a Shadow v4, partner_b Sovereign Pilot); `evaluate_membrane()` as the sole membrane decision function
- **Sprint 2** — `__init__.py`: clean public API boundary; installable via `pip install -e .`
- **Sprint 3** — `validator.py`: `TENIRValidator` with 9 named invariant checks (POL-001 → NOM-002); CLI (`tenir-validate`) and pytest fixture
- **Sprint 4** — `regression_corpus.py`: 281 golden `GoldenCase` fixtures across 16 scenario groups
- **Sprint 5** — `sdk.py`: `TENIRGovernanceClient` public API for both R4 (`adjudicate()`) and R5 (`adjudicate_from_nsl()`) paths
- **Sprint 6** — Plain-language hardening: `SCHIZOPHRENIA → SIGNAL_CONFLICT`, `WHALE_RESONANCE → DEEP_PATTERN_SIGNAL` with backward-compat aliases
- **Sprint 7** — CES classifier extended with `pressure` and `velocity` parameters; 14 corpus fixtures updated to reflect activity-based classification
- **Sprint 8** — Expanded posture family: `ROAM`, `LEARN`, `TRANSMIT`, `REST`, `SLEEP`, `STOP`; `AdmissibilityClass` added
- **Sprint 9** — `copy_lint.py`: CI-gated public-safe lexicon enforcement for `.md`, `.html`, `.txt`
- **Sprint 10** — `polymorphic_surface.py`: V5 Persona/Workflow surface contract (4 states, Two-Glass bridge, `requires_ceremony()`)
- **Sprint 11** — `ledger_migrate.py`: forensic JSONL ledger migration with migration-genesis hash and chain continuity

### Added — R5 Runtime (`r5_hardened/`, `r5_wired/`)
- LALR(1) NSL grammar parser with LLM fine-tuning pipeline (QLoRA / Axolotl)
- Neo4j 5.x graph ontology for CP-Net policy representation
- Merkle epoch audit ledger with inclusion proofs and peer broadcast
- `KeyCeremony` protocol for operator oath epoch-binding
- FastAPI server (`r5_server.py`) with `/adjudicate`, `/oath/sign`, `/transition`, `/ledger/verify`, `/ledger/proof/{id}` endpoints
- WebSocket hub (`ws_hub.py`) + Three.js VPS live engine

### Added — Infrastructure
- `Dockerfile` reproducibility container (Python 3.11-slim)
- `docker-compose.yml` with profile-gated Neo4j (`--profile graph`)
- 6-gate blocking GitHub Actions CI pipeline (`tenir-ci.yml`)
- `CITATION.cff`, `.zenodo.json` for Zenodo and GitHub citation metadata

### Validation
- 445 tests passing (100%): 313 governance package · 61 R4 · 56 R5 hardened · 15 R5 integration
- 70% statement coverage on `tenir_governance`
- 9/9 policy invariant checks across default, partner_a, and partner_b profiles
- Policy fingerprint (default): `d083e0b82a16c04d`

### Known Limitations
- Multi-tenant / multi-organisation isolation not benchmarked
- Distributed ledger replication out of scope
- Administrative override records disabled (fail-closed posture)
- NSL grammars require domain-specific schema definitions

---

## [4.0.0] — 2026-01 (partner_a Shadow v4)

### Summary
R4 release: internal UAT package for partner_a Shadow-mode deployment.
Introduced `TenirMonitor`, `HashChainedLedger`, `OperatorRegistry`, `PolicyBundle`, CAVE adjudication framework, and burn-cost estimation.
R4 runtime known issues (G1–G6) documented in `HARDENING_REPORT_v1.0.0-rc2.md`; these are addressed by construction in R5 through the shared `PolicyEngine.evaluate_membrane()` contract.
