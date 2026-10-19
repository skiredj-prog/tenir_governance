# TENIR-Gov: Governance Middleware for AI-Enabled Operational Systems

**Composing a Continuous Risk Monitor with a Stateful Invariant Enforcer to Govern Autonomous Agent Actions at the Execution Boundary**

*Abdelaziz Skiredj*

---

## Abstract

AI agents that take real-world actions — payments, transfers, API calls, infrastructure changes — need governance that operates at the boundary between authorization and execution. This paper presents TENIR-Gov, a governance middleware that composes two distinct enforcement mechanisms: a continuous risk-scoring kernel that evaluates operational pressure, and a stateful transition-system membrane that enforces discrete safety invariants. The composition produces a single governance plane that admits, holds, or vetoes agent actions before they reach external systems, and assesses what happened afterward.

The architecture rests on a fundamental distinction: **admissibility governs whether an action may proceed; post-execution assessment measures what happened and what value was preserved. Neither can substitute for evidence of the other.** This separation is structural, not contingent, and it shapes every component of the system.

We present the implemented system (733 passing tests, published on PyPI as `tenir-governance` v5.4.4), its doctrine, and the explicit boundary of its guarantees. The manuscript distinguishes throughout between what is doctrinally claimed, what is implemented and tested, and what is experimentally demonstrated — and is explicit about what remains unproven.

---

## 1. Introduction

### 1.1 The Problem

When an AI agent executes an action in an external system — charging a credit card, transferring funds, modifying a database, sending a message — two questions arise that conventional authorization systems do not adequately address:

1. **Was this action admissible?** Not merely "was the agent authorized to attempt it," but "did the action, in context, preserve the properties the system is meant to protect?"

2. **What actually happened?** Authorization is not execution. A governance decision to permit an action does not, by itself, prove that the action executed exactly once, or that its effects matched expectations, or that the value the system was designed to protect was actually preserved.

Existing systems typically conflate these questions. A policy engine authorizes; an execution layer executes; and the gap between the two — the interval where uncertainty lives — is treated as an implementation detail rather than a governance concern.

### 1.2 The TENIR-Gov Approach

TENIR-Gov is governance middleware that operates at this gap. It composes two enforcement mechanisms:

- **A scoring kernel** (`TenirKernel`) that computes a continuous risk score $S = K / (P \cdot V + \varepsilon)$ from operational inputs (pressure, velocity, capacity, authority, context fit, evidence quality, irreversibility materiality), producing a coarse admissibility signal.

- **A transition-system membrane** (`TransitionMembrane`) that enforces discrete invariants (I3–I9): delegation trust, budget limits, quarantine, terminality, trajectory, temporal constraints. These cannot be skipped based on a healthy score.

The two compose via a **decision lattice** that produces one of four dispositions: **PASS**, **FLAG**, **HOLD**, or **HARD\_VETO**. The composition is fail-closed: kernel exceptions, missing data, or indeterminate results produce HOLD, never PASS.

### 1.3 What This Manuscript Is — and Is Not

This is a **commercial manuscript**, not a conference paper. Its objective is *rentabilité* — commercial viability through market authority, sales enablement, and IP protection. It is written for technical buyers, architects, and engineering leaders who need to understand what TENIR-Gov protects, how it operates, and where its guarantees end.

The formal composition theorem and model checking of the state machine are identified as subsequent research tracks, not prerequisites for a commercially useful first edition. What this manuscript offers is an honest account of a working system, its doctrine, and its boundaries.

---

## 2. The Execution Boundary

### 2.1 The Interval Between Authorization and Outcome

The execution boundary is the interval between "the action was authorized" and "the outcome is known." TENIR-Gov isolates this interval and governs it directly. The membrane manages the lifecycle of execution attempts *after* they have been admitted, producing dispositions that account for uncertainty rather than assuming it away.

The key insight is that a governance decision and an execution outcome are different things. "Permitted to execute" does not mean "successfully executed." A timeout does not prove no effect occurred. A failed response does not prove the operation did not reach the external system. The membrane treats these distinctions as first-class concerns, not edge cases.

### 2.2 The Dispatch State Machine

**Implemented behavior.** The dispatch state machine governs the lifecycle of each operation through the following states:

```
CREATED → HOLD → RESERVED → DISPATCHING → CONFIRMED | FAILED | UNKNOWN | ESCALATED
```

Key properties of the implementation:

- **Idempotency key is durably claimed at CREATED.** Before any dispatch occurs, the operation's idempotency key is claimed in a durable registry. This prevents duplicate adjudication even if the caller retries.

- **CREATED blocks fresh adjudication.** Once an operation exists in the registry, a new adjudication for the same key is rejected. The system uses `resume_existing()` to recover in-progress operations rather than starting over.

- **Timeout produces UNKNOWN, not FAILED.** A dispatch timeout does not prove the external effect did not occur. UNKNOWN is a reconcilable state — it can transition to CONFIRMED or FAILED via `reconcile_unknown()` when the provider returns evidence.

- **UNKNOWN is not terminal.** Only CONFIRMED and FAILED are terminal. UNKNOWN can be reconciled, ESCALATED can be exited (with authorization), and HOLD can be resolved.

- **FAILED is defined narrowly.** FAILED means there is evidence that the effect did not occur. Absence of evidence is not evidence of non-execution.

### 2.3 The Provider Abstraction

**Implemented behavior.** The `DispatchProvider` protocol defines the interface between governance and execution:

```python
class DispatchProvider(Protocol):
    def execute(self, operation, idempotency_key, metadata) -> DispatchResult: ...
    def reconcile(self, operation, idempotency_key) -> ReconciliationResult: ...
```

- `MockProvider` — for testing and demonstration.
- `StripeProvider` — adapter for Stripe, using the governance-assigned idempotency key as the Stripe `Idempotency-Key` header for provider-side deduplication.

The provider receives the governance-assigned idempotency key, not one it generates. This ensures that the governance layer's idempotency tracking and the provider's deduplication are aligned — a requirement identified by Helland's work on exactly-once effects across service boundaries.

---

## 3. Cross-Realm Transitions and Property-Preserving Crossing

### 3.1 The Fundamental Non-Implication

**Doctrine.** The central doctrinal insight is a structural non-implication:

$$\text{Admissible}(a \mid R_A) \;\not\Rightarrow\; \text{Admissible}(\tau : R_A \to R_B)$$

Local authorization of an operation within a source Realm does not imply admissibility of the transition it produces when crossing into a target Realm. Crossing can alter: reversibility class, coupling depth, temporal horizon, material risk surface, effective authority, standing validity, and recovery capacity.

This is **structural, not contingent**. It is not a statement about buggy systems or misconfigured policies — it is a statement about the nature of cross-Realm transitions. The object of evaluation changes at the boundary.

### 3.2 The Property-Preserving Crossing Contract

**Doctrine.** A governed transition $\tau : R_A \to R_B$ is evaluated against a crossing contract:

$$K_\tau = (\text{Pre}_\tau,\; I_A,\; I_B,\; B_\tau,\; \text{Auth}_\tau,\; \text{Ev}_\tau,\; \text{Fresh}_\tau)$$

Where $\text{Pre}_\tau$ = entry preconditions; $I_A, I_B$ = source/target invariants; $B_\tau$ = correspondence relation giving meaning to "preserving" between heterogeneous states; $\text{Auth}_\tau$ = required authority; $\text{Ev}_\tau$ = required evidence; $\text{Fresh}_\tau$ = freshness condition at commit.

The passage is property-preserving relative to $K_\tau$ if:

$$PP(\tau, K_\tau) \iff \forall s_A \in \text{Reach}_A, \forall s_B \in S_B : \text{Pre}_\tau(s_A) \wedge I_A(s_A) \wedge (s_A, s_B) \in T_\tau \Rightarrow \text{Auth}_\tau(s_A) \wedge \text{Valid}(\text{Ev}_\tau, s_A) \wedge \text{Fresh}_\tau(s_A) \wedge B_\tau(s_A, s_B) \wedge I_B(s_B)$$

This definition covers all possible outputs of the transition, not just the nominal path. It expresses a **safety property, not a progress guarantee**. The contract is relative: it guarantees only the predicates written in $K_\tau$, within the limits of their model and observability.

### 3.3 Implementation Status

**Implemented behavior.** The membrane enforces invariants I3–I9 (delegation trust, budget limits, quarantine, terminality, trajectory, temporal constraints). The `DualKernelGovernor` composes the scoring kernel's continuous assessment with the membrane's discrete invariant checks via a decision lattice.

**What is not yet implemented.** The full crossing contract $K_\tau$ with explicit correspondence relations $B_\tau$ is doctrinally specified but not yet fully implemented as a first-class object. The current implementation evaluates invariants at the transition boundary but does not yet support heterogeneous state correspondence between Realms.

---

## 4. Evidence and Uncertainty

### 4.1 Three Epistemic Boundaries

**Doctrine.** The architecture recognizes three epistemic boundaries that constrain what governance can know:

**E1 — Ambiguity of silence.** A timeout, network partition, lost response, or unacknowledged dispatch leaves execution status unknown. Silence must not be treated as proof of non-execution. It cannot justify FAILED or admission of a subsequent attempt.

**E2 — Attestation reliance.** Reasoning that relies on a non-execution attestation depends on the attestation being truthful. A cryptographically authenticated statement can still be false about the external world. Authentication and integrity do not entail truthfulness.

**E3 — The concealment limitation.** A specific boundary condition where: (1) an attempt produces a real external effect the membrane does not observe; (2) a false non-execution attestation permits the attempt to be declared FAILED; (3) the subsequent attempt is dispatched and produces another real effect for the same logical operation. This can violate the at-most-once-effect invariant.

### 4.2 Fail-Closed Composition

**Implemented behavior.** The decision lattice composes the scoring kernel and membrane results. The composition is fail-closed:

- Kernel exceptions → HOLD
- Missing data → HOLD
- Indeterminate results → HOLD
- A healthy score does NOT bypass mandatory invariant checks

The mandatory invariants (I3, I4, I6, I7, I8) cannot be skipped based on a favorable risk score. This is a deliberate design choice: a healthy score does not prove invariants hold, and skipping checks based on a score would collapse the distinction between continuous assessment and discrete enforcement.

### 4.3 The At-Most-Once-Effect Invariant

**Doctrine.** The functional safety property is at-most-once effect (I1): for each logical operation identifier $L$ and every finite execution-history prefix $\pi$:

$$I1(\pi): |E_L(\pi)| \leq 1$$

Where $E_L(\pi)$ denotes the number of externally effective outcomes attributable to $L$. This property concerns **real external effects, not merely effects recorded in the membrane's local state**.

**Conditional guarantee.** The conditional refinement theorem (Theorem 1 in *Governing the Unobserved*) states that I1 holds where stated assumptions and transition obligations are satisfied. This is a **conditional theorem** — its hypotheses must be checked against the actual transition relation. It is not an unconditional guarantee for arbitrary external systems.

**Experimentally demonstrated.** Bounded exhaustive exploration found no I1 violation in honest, false-attest, and hidden-honest worlds within their depth limits. An adversarial model found a three-transition counterexample under E3 conditions. These are bounded results, not universal proofs.

---

## 5. The Two-Kernel Architecture

### 5.1 The Scoring Kernel

**Implemented behavior.** The `TenirKernel` computes a risk score:

$$S = \frac{K}{P \cdot V + \varepsilon}$$

Where $K$ = capacity (knowledge/resources), $P$ = pressure (urgency/demand), $V$ = velocity (rate of change), and $\varepsilon$ prevents division by zero. The kernel also incorporates authority, context fit, evidence quality, and irreversibility materiality into its assessment.

The scoring kernel produces a continuous signal that feeds into the decision lattice. It does not directly authorize or veto actions — it contributes one input to the composition.

### 5.2 The Transition-System Membrane

**Implemented behavior.** The `TransitionMembrane` enforces discrete invariants:

| Invariant | Description | Type |
|-----------|-------------|------|
| I3 | Attempt terminality — passage already terminal → HARD\_VETO | MANDATORY |
| I4 | Only dispatch produces effects — passage not in PENDING/HELD state → HARD\_VETO | MANDATORY |
| I6 | No admission under quarantine — QUARANTINE\_LOCK active → HARD\_VETO | MANDATORY |
| I7 | Delegation integrity — delegating agent not registered or cannot delegate → HARD\_VETO | MANDATORY (when delegation in scope) |
| I8 | Aggregate budget — available < requested amount → HARD\_VETO (atomic under per-resource lock) | MANDATORY (when resource in scope) |
| I9 | Trajectory temporal consistency — temporal constraint violated → HARD\_VETO | MANDATORY (when trajectory in scope) |

The membrane also supports `QUARANTINE_LOCK`, which blocks ALL admission and dispatch for a legal entity identifier (LEI) until authorized release. This is distinct from HOLD: QUARANTINE\_LOCK is an LEI-level governance state; HOLD is a per-operation disposition.

### 5.3 The Decision Lattice

**Implemented behavior.** The `compose()` function in the decision lattice takes the scoring kernel's result and the membrane's invariant checks and produces one of four dispositions:

- **PASS** — admissible; transition to RESERVED
- **FLAG** — admissible with caution; transition to RESERVED (like PASS, but posture is CAUTION)
- **HOLD** — withhold admission; evidence or governance question unresolved
- **HARD\_VETO** — inadmissible; no path to execution

FLAG transitions to RESERVED (like PASS), but if budget utilization exceeds the configurable escalation threshold (default 85%), FLAG escalates to HOLD. The `CompositionPolicy` is frozen, versioned, and configurable.

### 5.4 The Governor

**Implemented behavior.** The `DualKernelGovernor` bridges the two kernels via `ScoringKernelAdapter` (which wraps the existing `TenirKernel` — "bridge, don't replace"). It provides:

- `adjudicate()` — compose scoring + membrane, produce decision
- `dispatch_and_execute()` — dispatch to provider, record outcome
- `reconcile_unknown()` — reconcile UNKNOWN states via provider
- `resume_existing()` — recover in-progress operations after crash
- `assess_post_execution()` — opt-in post-execution assessment (τ⁻¹ and EVP)

---

## 6. HOLD as Governed State

### 6.1 Doctrine: HOLD Is Not Failure

**Doctrine.** HOLD is a first-class protocol state — a designed suspension of agency under pressure, not a system failure. It withholds admission for the decision at issue while the evidence or governance question remains unresolved. A later decision must be based on admissible evidence and the applicable transition rules.

This distinction is central to TENIR's identity. HOLD is not an error state or a fallback — it is a governance disposition that says "the system is deliberately not proceeding because it should not."

### 6.2 Implementation

**Implemented behavior.** The `HoldManager` implements the full HOLD lifecycle:

- `enter_hold()` — transition operation to HOLD state
- `resolve_hold()` — re-adjudicate with new evidence
- `escalate_hold()` — escalate to ESCALATED when $\tau_K$ expires
- `authorize_exit()` — authorized exit from ESCALATED with `ExitReceipt`
- `expire_hold()` — $\tau_K$ expiry produces UNKNOWN, not resolution
- `expire_escalated()` — ESCALATED expiry produces UNKNOWN

Key properties:

- **$\tau_K$ is derived from organizational SLAs**, never invented. The temporal validity bound comes from existing service-level agreements.
- **ESCALATED is absorbing.** No time-based exit exists. An authorized exit requires an `ExitReceipt` capturing who signed, scope, authorization mechanism, and reason.
- **$\tau_K$ expiry → UNKNOWN**, not resolution. Time does not resolve a governance question.

### 6.3 Composition-Level HOLD Strategies

**Implemented behavior.** The `CompositionHoldManager` provides five strategies for handling HOLD at the composition level:

1. **Priority-Enforce** — resolve the highest-priority HOLD first
2. **Serialize** — resolve HOLDs in order
3. **Pareto-Restore** — resolve the HOLD that restores the most properties
4. **Escalate** — escalate to ESCALATED
5. **Fail-Closed-Collectif** — fail closed across all affected operations

Progressive unblock (Déblocage progressif) verifies invariants before each TAU release, unblocking one at a time rather than all at once.

---

## 7. τ⁻¹ — Partial Tau Inversion

### 7.1 Doctrine

**Doctrine.** τ⁻¹ is a **partial, conditional, property-relative inverse** — not a full mathematical inverse. Let $Q \subseteq \Pi$ be the set of relevant return properties and $\approx_Q$ an equivalence relation on states comparing only those properties. For a departure subset $D \subseteq R_A$, τ⁻¹ exists if, for each state $s \in D$ and each authorized result $s'$ of $\tau(s)$, a return transition $\rho$ exists such that:

$$\rho = \tau^{-1}_Q, \quad \rho(\tau(s)) \approx_Q s$$

This definition does not claim to restore the entire world state. It asserts only the return of the explicitly retained properties $Q$. When multiple effects or results render the transition non-deterministic, the existence of an inverse must be established for the set of all relevant results, or the limitation must be signaled.

### 7.2 The Loss Function

**Doctrine.** The loss function measures the quality of the return:

$$L_Q(s, \tau, \rho) = \text{distance}_Q(\rho(\tau(s)), s) + \lambda \cdot \text{cost}(\rho)$$

Where $\text{distance}_Q$ measures the gap on retained properties; $\text{cost}(\rho)$ measures resources, delays, and side effects of the return; $\lambda$ is a context-specific weight balancing fidelity and cost.

This is explicitly described as **only a proposed operationalization**. In contexts involving fundamental rights or strict constraints, an average or monetary value must not compensate for the violation of an absolute property.

### 7.3 Four Doctrine Classifications

**Doctrine.** The Gateway distinguishes:

1. **Pre-commit cancellation** — the action has not yet produced an external effect
2. **Exact inverse on Q** — the inverse operation restores the targeted properties
3. **Compensation** — an operation reduces some effects without restoring the initial state
4. **Irreversibility** — no authorized operation satisfies the chosen return criterion

### 7.4 Implementation

**Implemented behavior.** The `TauInversionClassifier` implements five classifications — the four doctrine classifications plus **INDETERMINATE** for cases where the assessment cannot be completed:

- **PRE\_COMMIT** — requires explicit confirmation that no external effect occurred (`no_external_effect=True`). Absence of evidence is not evidence of non-execution.
- **EXACT\_INVERSE** — return transition restores all properties in $Q$ (distance\_Q = 0)
- **COMPENSATION** — return transition reduces effects but does not fully restore (distance\_Q > 0)
- **IRREVERSIBLE** — no return transition is available
- **INDETERMINATE** — assessment cannot be completed (insufficient information, callback failure, invalid numeric inputs)

Key semantic decisions (hardened through peer review):

- **`no_external_effect` defaults to None (indeterminate)**, not True. A failed operation without explicit confirmation that no effect occurred returns INDETERMINATE, not PRE\_COMMIT. Absence of evidence is not evidence of non-execution.
- **Empty $Q$ returns INDETERMINATE.** A zero-distance callback with no retained properties does not establish meaningful reversibility. An empty property set makes the assessment vacuous.
- **Callback exceptions return INDETERMINATE**, not IRREVERSIBLE. A failed assessment does not prove that no authorized inverse exists — it means the assessment could not be completed.
- **Numeric inputs are validated.** Distance, return cost, and $\lambda$ must be finite and non-negative. NaN, infinite, or negative values return INDETERMINATE.

### 7.5 Assessment Lifecycle

**Implemented behavior.** The `PostExecutionAssessor` composes τ⁻¹ and EVP into a single `AssessmentReport`. The assessor is **opt-in** — `dispatch_and_execute()` does not auto-trigger assessment. Assessment is an explicit follow-up action via `assess_post_execution()`.

Limitations documented in the implementation:
- Reports are stored in-memory (no restart persistence, no tamper evidence)
- `return_transition` is an assessment callback only, not a compensating action
- The assessment path does not perform irreversible operations

---

## 8. EVP — Intended Value Preservation

### 8.1 Doctrine

**Doctrine.** EVP measures intended value protection as an **outcome** of governance discipline, not as an input to the admissibility decision. The critical separation is:

$$\text{EVP} \notin \text{Admissibility Function}$$

Admissibility precedes value measurement. High expected value never justifies an otherwise inadmissible transition. EVP Gross, EVP Net, friction costs, and Governance Debt are reported as **consequences**, not as inputs to the decision function $D$.

EVP is neither a universal utility function nor a synonym for compliance. Compliance asks whether an action respects rules; reversibility asks whether certain properties can be restored; EVP asks whether the transition preserves the legitimate purpose and beneficiaries the system was meant to serve.

### 8.2 The Frozen Equations

**Implemented behavior.** The `EVPAssessor` computes:

$$\text{EVP Gross} = \text{intended\_value} \times \text{confidence}$$

$$\text{EVP Net} = \text{realized\_value} - \text{execution\_loss}$$

$$\text{EVP Ratio} = \frac{\text{EVP Net}}{\text{EVP Gross}}$$

**Friction costs are reported separately** — they are not subtracted from EVP Net. This ensures the definition, formula, ratio, and examples all agree. Friction is a governance cost worth tracking, but it does not reduce the value that was actually realized.

### 8.3 Non-Implication Between τ⁻¹ and EVP

**Doctrine.** τ⁻¹ and EVP do not imply each other:

- A transition can be reversible (τ⁻¹ exists) while destroying intended value (poor EVP) — e.g., canceling an action after revealing confidential information.
- A transition can be irreversible (no τ⁻¹) while preserving intended value (good EVP) — e.g., an irreversible action that achieves exactly what it was designed to achieve.

Both are **ex-post** (post-execution) concepts. They run after execution, not before. They assess what happened, not what should be permitted.

---

## 9. Explicit Limitations and the Guarantee Boundary

### 9.1 Three Tiers of Guarantee

The architecture publishes its guarantee boundary in three explicit tiers:

**Locally guaranteed (within the model and stated assumptions):**
- Deterministic gate composition and fail-closed behavior under missing evidence
- The conditional refinement theorem holds where transition obligations and assumptions are satisfied
- 733 passing tests covering composition, membrane, registry, provider, HOLD lifecycle, τ⁻¹, EVP, assessor, and package integrity

**Requires distributed infrastructure:**
- Safety under unconstrained asynchronous concurrency — not established
- Correctness of a distributed lock — not established
- Crash recovery of membrane state — not established
- General thread safety beyond the targeted two-thread admission race test

**Depends on external provider behavior:**
- Legitimacy of caller-supplied public keys — not established
- Truthfulness of external-world attestations — not established (assumption A)
- Observation completeness (assumption O) — assumed, not guaranteed by the membrane
- Exactly-once effect across service boundaries — requires provider-side deduplication

### 9.2 What Is Explicitly Not Established

- General safety under concurrency
- Distributed lock correctness
- Legitimacy of caller-supplied public keys
- Truthfulness of external-world attestations
- Crash recovery
- Comparative effectiveness against deployed systems
- Any property of the scalar kernel score
- An authorized exit from non-terminal ESCALATED states (by design — ESCALATED is absorbing)
- Production readiness

### 9.3 The Honest Boundary

The architecture **declares the boundary rather than claiming a guarantee it cannot support**. Where evidence cannot establish non-execution, or where the environment can conceal an effect while supplying false non-execution evidence, the system says so. It does not paper over the gap with optimistic defaults.

This is the guiding distinction that shapes the entire system:

> **Admissibility governs whether an action may proceed. Post-execution assessment measures what happened and what value was preserved. Neither can substitute for evidence of the other.**

---

## 10. Product Tiering

**Future work.** The product is envisioned in three tiers:

| Tier | Scope | Delivery |
|------|-------|----------|
| **Tier 1** | Scoring kernel only | Low-cost SaaS |
| **Tier 2** | Membrane only | Medium SaaS |
| **Tier 3** | Both kernels + composition | Enterprise license |

This tiering reflects the architectural separation: the scoring kernel and the membrane are independently useful. A organization may need continuous risk assessment without the full dispatch state machine, or may need the invariant enforcement without the scoring layer.

---

## 11. Future Research Tracks

The following are identified as subsequent research tracks, not prerequisites for commercial viability:

1. **Formal composition theorem** — A defensible formal contract between the scoring kernel and the membrane, with explicit assumptions under which the composition preserves the safety guarantees of both.

2. **Model checking of the state machine** — Verification that the dispatch state machine has no reachable forbidden states (e.g., CONFIRMED → DISPATCHING, or terminal state mutation).

3. **Epistemic abandonment as a membrane invariant** — Formalizing the principle that the membrane must not infer non-execution from silence as a checkable invariant.

4. **Empirical validation of scoring thresholds** — The $S > 2 \times \text{alert\_floor}$ skip criterion needs validation against real operational data.

5. **Ed25519 signatures** — The ledger currently uses HMAC. OVERT compliance requires Ed25519 signatures for tamper evidence.

6. **Exactly-once effect with real infrastructure** — Testing the provider abstraction against a real payment provider (Stripe) to validate the idempotency key flow end-to-end.

---

## 12. Conclusion

TENIR-Gov is a working governance middleware that composes a continuous risk monitor with a stateful invariant enforcer to govern autonomous agent actions at the execution boundary. It is published on PyPI, tested with 733 passing tests, and demonstrated through a 10-minute buyer-facing demo.

The system's value proposition is not that it solves all governance problems — it does not. Its value is that it **makes the boundary explicit**: between authorization and execution, between admissibility and value, between what is guaranteed and what is not. It governs the gap that other systems ignore, and it does so with an honest account of what it can and cannot prove.

The doctrine distinguishes admissibility from post-execution assessment. The implementation enforces this distinction through fail-closed composition, mandatory invariant checks, and opt-in assessment. The guarantee boundary is published in three tiers, and the limitations are stated explicitly.

This is the foundation for a commercial product: not a claim of universal safety, but a credible, tested, and honest governance layer that operates where it matters most — at the execution boundary.

---

## Appendix A: System Versions

| Version | Date | Scope |
|---------|------|-------|
| v5.3.0 | 2026-10-12 | Two-kernel composition (Agentic Governance Profile v1.0.0) |
| v5.3.1 | 2026-10-13 | Patch: provider integration, security, demo |
| v5.4.0 | 2026-10-13 | Minor: τ⁻¹ and EVP assessment layer |
| v5.4.1 | 2026-10-13 | Patch: assessment semantics hardening |
| v5.4.2 | 2026-10-18 | Patch: I8 budget coordination fix |
| v5.4.3 | 2026-10-19 | Patch: CAVE doctrinal alignment, metadata fixes |

**PyPI:** `pip install tenir-governance`
**Repository:** `github.com/skiredj-prog/tenir_governance`

## Appendix B: Test Coverage

| Test File | ~Tests | Coverage |
|-----------|-------|----------|
| `test_composition.py` | ~61 | Decision lattice composition, budget escalation, hash-chained ledger, DualKernelGovernor end-to-end, ScoringKernelAdapter bridge, schema conformance |
| `test_membrane.py` | ~18 | I3 terminality, I4 dispatch-only effects, I6 quarantine, I7 delegation, I8 budget (atomic), I9 trajectory, uncertain outcome reconciliation |
| `test_objects.py` | ~18 | Agent, Passage, Trajectory, Resource, Delegation, MissionInvariant models |
| `test_hold_lifecycle.py` | 57 | HOLD enter/resolve/escalate/expire, authorized exit, τ\_K expiry, governor integration, QUARANTINE\_LOCK, 5 composition strategies, progressive unblock |
| `test_provider.py` | 32 | MockProvider, dispatch\_and\_execute, timeout→UNKNOWN, reconciliation, full lifecycle, recovery |
| `test_tau_inversion.py` | 56 | All 5 τ⁻¹ classifications, loss function, P1 semantic fixes, numeric validation, non-determinism |
| `test_evp.py` | 34 | EVP Gross/Net/Friction/GovernanceDebt, frozen-equation verification, separation from admissibility |
| `test_assessor.py` | ~27 | PostExecutionAssessor: τ⁻¹+EVP combined, non-interference with admissibility, report storage |
| `test_peer_review_fixes.py` | ~24 | P1-1 persistence default, P1-2 corrupted registry, P1-3 hash chain, P2-1 no favorable defaults, P2-2 RESERVED transition |
| `test_cross_process.py` | ~5 | Cross-process registration uniqueness, dispatch atomicity |
| `test_fault_injection.py` | ~13 | State machine legal/illegal transitions, crash-after-transition, durable registry persistence |
| `test_policy_edge_cases.py` | ~6 | Policy validation, version drift guard, R4 bundle export |
| `test_policy_governance_guarantees.py` | ~6 | Determinism guarantee, boundary determinism, legacy coverage |
| `test_regression_corpus.py` | ~43 | Policy engine contract, membrane invariants, nomenclature, validator CI gate, golden corpus regression |
| `test_sprints_9_10_11.py` | ~32 | Polymorphic surface, two-glass bridge, opacity map, copy-lint, ledger migration |
| **Total** | **733** | **0 failures, 0 regressions** |

## Appendix C: Doctrine References

1. *Property-Preserving Crossing for Agentic Systems* — τ⁻¹ formal definition, four classifications, loss function, crossing contract
2. *TENIR Property-Preserving Transitions Manuscript* — EVP as downstream consequence, admissibility envelope, gate architecture
3. *Governing the Unobserved v7.1* — Execution boundary, at-most-once effect, invariants I1–I8, conditional refinement theorem, epistemic boundaries, HOLD lifecycle
4. *Governing the Unobserved v7.0* — Execution boundary, unobserved operations, governance decisions vs. execution outcomes

## Appendix D: Key Research Foundations

- Pinisetty et al. (2021, 2016) — Runtime verification
- Falcone et al. (2011) — Runtime enforcement
- Leucker & Schallhart (2009) — Runtime verification overview
- Schneider (2000) — Enforceable security policies
- Alpern & Schneider (1985) — Safety and liveness
- Gray & Reuter (1992) — Transaction processing
- Helland (2007) — Life beyond distributed transactions
- NIST AI RMF — AI risk management framework
- Zhuang et al. (2023) — ExoFlow
