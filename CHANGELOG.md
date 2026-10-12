# Changelog

All notable changes to TENIR-Gov are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased] — HOLD Lifecycle

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
