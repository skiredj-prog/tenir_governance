# REG-RC-01 — Reconciliation & Recovery State Machine

**Candidate v0.1 · 2026-10-26 · Proposed normative annex, NOT ratified · PROOF-01 lab only**

## 0. Normative position and scope

This document proposes *testable obligations*. **MUST/SHALL below mean requirements of THIS CANDIDATE**, not obligations adopted by RFC-0..7 or any law. The frozen RFCs are not amended here. REG-RC-01 defines a boundary contract, not a prescribed TENIR-Gov internal algorithm. TENIR-Gov serves **systemic viability** under the TENIR canon, not veto maximization.

Three distinct facts MUST NOT be conflated: (i) an authorization decision or grant; (ii) a **local** durable operational record, including budget ownership; (iii) the **provider-attested external outcome**. In particular, a valid Commit grant does **not** prove an external effect and a local timeout does **not** prove its absence.

### Trust boundaries

- `Standing / Structure / Commit` authorizes an exact attempted action; the provider's ledger attests only what that provider can establish for that exact idempotency key and scoped action.
- The verifier independently checks cryptography *relative to pinned trust material*; it cannot prove a provider's representations about the external world are true.
- The checkpoint authority MUST have a declared threat model, process/credential boundary, epoch and durability/rollback domain. An on-disk mirror controlled by the same UID is not independent.
- Executing any consequential effect **outside** the sole enforceable ingress is B1 failure. The provider's credentials, identity policy and network ACLs are required parts of the claimed deployment boundary, not things REG-RC-01 can prove from application code alone.

## 1. State model — three orthogonal dimensions

A single flat enum is insufficient. Record at minimum the following dimensions and correlate them by immutable `operation_id`, `action_digest`, `attempt_id`, `grant_id`, `provider_id`, `idempotency_key`, `authority_epoch`:

**Authorization:** `UNVERIFIED | VALID_BOUND | CONSUMED | EXPIRED | REVOKED` (not a provider outcome).

**External outcome epistemic state:** `NOT_SENT_ESTABLISHED | MAY_HAVE_BEEN_SENT | UNKNOWN | EFFECT_CONFIRMED | NO_EFFECT_FINAL` (provider fact only when verified).

**Local settlement:** `HELD | COMMITTED | RELEASED | REPAIR_REQUIRED` (independently reconciled with the provider fact).

The following are **named composite views** for operators; they MUST NOT erase the orthogonal dimensions:

| View | Required meaning | Terminal? |
|---|---|---|
| `RESERVED` | Durable resource reservation; no dispatch recorded | Nonterminal |
| `DISPATCHING` | Durable dispatch **intent** and grant consumption; send may or may not have happened | Nonterminal |
| `UNKNOWN` | Outcome not established after possible dispatch, including crash/lost ACK | Nonterminal |
| `CONFIRMED` | Provider's positive proof verified AND local settlement COMMITTED | Terminal outcome for this attempt |
| `FAILED` | Qualified provider negative-final proof verified AND local settlement RELEASED | Terminal outcome for this attempt |
| `RECONCILED_CONFIRMED` | As CONFIRMED, reached by recovery of UNKNOWN | Terminal outcome for this attempt |
| `RECONCILED_NOT_EXECUTED` | As FAILED, reached by recovery of UNKNOWN | Terminal outcome for this attempt |
| `REVIEW_REQUIRED` | Supervisory **flag** on an unresolved UNKNOWN; not a terminal outcome | **Nonterminal** |
| `REPAIR` | Verified external fact exists but the matching local accounting/anchor has not completed | Nonterminal; no replay |

`REPAIR` can originate directly from a verified provider receipt after any failed local settlement; it must **not** require first falsely declaring `CONFIRMED` as terminal. `REVIEW_REQUIRED` may coexist with `UNKNOWN` and must be revisited with new admissible evidence.

## 2. Transition and evidence table

Every transition requires a compare-and-set over an immutable operation key and an expected version/epoch. No row below allows grant reuse, alternate dispatch or unapproved budget release.

| From → To | Guard and permitted action | Durable artifacts / actor | Failure/crash outcome |
|---|---|---|---|
| `RESERVED → DISPATCHING` | Valid unexpired scoped grant; authority owner; active epoch; remaining reserved budget; persist intent **before** provider call | Owner: bound dispatch intent, grant consumption, attempt/nonce/idempotency bindings, monotone checkpoint protocol | If receipt unavailable or crash after intent, presume **possible send**; read state and reconcile; never issue blind retry |
| `DISPATCHING → CONFIRMED` | Scoped **positive final** provider evidence cryptographically verified; no equivocation; budget settlement durable | Local authority: receipt ref, checkpoint, same-transaction `COMMITTED` and terminal operation, evidence manifest | If local settlement/anchor fails: `REPAIR`; retain provider fact and reservation |
| `DISPATCHING → FAILED` | Qualified **negative final** proof; provider has a durable tombstone/finality mechanism that prohibits future effect for the same key | Local authority: negative receipt ref, epoch, same-transaction `RELEASED` and terminal operation | Failed local settlement ⇒ `REPAIR`, not `FAILED` |
| `DISPATCHING → UNKNOWN` | Lost ACK/timeout/crash, missing, stale or contradictory proof | Local authority: durable uncertainty reason, dispatch intent, retained reservation and proof pointers | Keep `UNKNOWN`; absence of reply is not absence of effect |
| `UNKNOWN → RECONCILED_CONFIRMED` | Positive proof verified for same exact request, provider and epoch; local commit settled | Durable receipt, atomic budget settlement, evidence and checkpoint | If incomplete ⇒ `REPAIR` |
| `UNKNOWN → RECONCILED_NOT_EXECUTED` | Negative-final proof with authority to rule out *future* effect for bound key, plus locally completed release | Durable receipt, atomic budget release, evidence and checkpoint | If incomplete ⇒ `REPAIR` |
| `UNKNOWN → REVIEW_REQUIRED` | Contradiction, stale result, missing witness, exhaustion of **review** SLA; no adjudicable final proof | Escalation owner, signed reason and due time; **UNKNOWN retained** | Fail closed on new effect; continue restricted reconciliation |
| `REVIEW_REQUIRED → UNKNOWN` | Reassessment or new evidence, without new execution grant | Auditable reassessment event + case owner | No effect, no budget release on status change |
| `REPAIR → RECONCILED_*` | Original provider fact pinned, local CAS settlement and checkpoint succeed; independent verification of replay safety | Idempotent local settlement, repair receipt, retained original provider receipt | If failure, remain `REPAIR`; no second provider effect |
| Terminal → new consequential attempt | **New** Standing/Structure/Commit context AND fresh grant/nonce; policy independently validates feasibility | New `attempt_id` and authorization chain; previous attempt immutable | No implicit resurrection of old grant; original provider key's idempotence remains enforced |

**Budget ownership:** When a provider effect is established but local settlement is incomplete, keep the resource accounted for as held/repair, *not* released. A terminal view requires local accounting consistency. A single local transaction may atomically update budget and operation state; it cannot imply atomicity with external provider or remote witness.

## 3. Witness-first without false distributed atomicity

A monotone external witness and a local SQLite transaction do not share an atomic commit by default. The phrase "witness-first, inseparable" is therefore **a proof obligation**, not an implementation fact.

Recommended **candidate** recovery protocol:

1. External witness durably allocates/accepts a single-use intent `(operation, version, epoch, intent_digest)` and returns an authenticated token.
2. Local authority, under mandatory owner fencing and conditional version, durably writes the identical token and transitions its **local** state. An orphan witness intent cannot authorize a provider call.
3. Witness advances monotonically to `ACTIVATED/COMPLETED` after verifying the bound local receipt. Success is acknowledged only after requisite witnesses and local commits are proven; ambiguity leads to HOLD/UNKNOWN/REPAIR.
4. Any `PREPARED` intent, divergent digest, rollback/erasure or unreachable witness **blocks new dispatch**, and requires bounded reconciliation before recovery.

Steps 1–3 are **not one atomic cross-store transaction**. This protocol must be tested against every crash cut and partitions; it remains a design proposal, not established implementation. A positive local acknowledgement is forbidden before all required durable conditions. For Q1/Q6, a missing or old local marker cannot grant authority if independently pinned witness epoch is newer or unreachable.

## 4. Negative evidence and provider finality

A successful provider database query returning no row is **not** proof of non-execution. Qualified negative proof MUST attest:

- Exact `provider_id`, `operation_id`, `idempotency_key`, action digest and bounded scope.
- **Coverage and finality:** provider is authoritative for *all* effects under the key, and atomically commits a terminal **non-execution tombstone** (or equivalent closed-world finality) that makes later execution of the same key impossible.
- Evidence of query consistency, finality time, authority and receipt signing identity. A status `NOT_FOUND` without durable tombstone stays `UNKNOWN`.
- Revocation and contradiction handling must never silently prefer a convenient receipt.

The laboratory fixture in this package implements this finality **inside its own SQLite ledger only**. It cannot prove absence of side effects in a bank or any system outside the fixture.

## 5. Timing and viability

**`τ_K` (RFC-5)** is the declared organizational veto/review window for a `HOLD` disposition. Do **not** silently reuse it as an execution-result-resolution SLA for `UNKNOWN`. An implementation MAY expose separately declared `τ_reconcile` / `τ_review`, with source SLA, owner, expiry policy and evidence. Expiry of any such interval **never** turns `UNKNOWN` into `FAILED`, confers authority or releases budget.

Required PROOF-01 measures: `unknown_duration`, `reconciliation_time`, `review_backlog`, `capacity_held_during_unknown`, `capacity_restored_after_proven_resolution`, `recovery_options`, `tau_k_remaining_if_applicable`, and count of blocked unsafe repeats. Neither maximum block count nor maximum execution throughput is an objective by itself.

## 6. Security controls and conformance status

Q1 = deletion of quarantine AND initialization marker; Q6 = rollback to previously genuine released-quarantine state after newer incident. Both are **Safety / State Integrity** negative vectors. `Recovery Integrity` spans anti-rollback and valid reauthorization; `Liveness / Operational Recovery` measures resumed viable capacity. Propose a **cross-reference** in frozen draft RFC-5 §4, and new tests in RFC-7; DO NOT edit frozen RFCs automatically.

Two W1 proof obligations remain OPEN:
- `W1-BYPASS-01`: raw G4-A admission bypasses optional checkpoint wrapper.
- `W1-PRIV-01`: same-UID process can rewrite checkpoint and local evidence state.

The provider fixture added here addresses **B3 laboratory receipts**. It does **not** close B1, B2, W1-BYPASS-01, W1-PRIV-01 or B4. Its Merkle checkpoint is signed **by the provider**, not an independent third-party anchor; out-of-band pinning must be supplied by an auditor or separate operator. Same-principal key theft and equivocation remain outside the demonstrated boundary.

## 7. Relationship to frozen draft RFCs

- **RFC-1:** EGE, idempotency, sequence and timeouts never authorizing an unverified execution.
- **RFC-2 §2.5:** UNKNOWN as epistemic condition, not an affirmative PASS.
- **RFC-4:** signed decision receipt ≠ authenticated external effect receipt; explicit post-commit evidence and no claimed distributed atomicity.
- **RFC-5 §4:** recovery requires new Standing, Structure and Commit authorization; no resumed old grant.
- **RFC-6:** cryptographic identity/pinning and workloads; fixture does not claim mTLS conformance.
- **RFC-7:** versioned adversarial vectors and scope-sensitive conformance.

**Final decision:** `CANDIDATE FOR REVIEW / E2E FUNCTIONAL NON-CERTIFYING / PRODUCTION HOLD`. No RFC ratification or TENIR-Gov repository change is implied.
