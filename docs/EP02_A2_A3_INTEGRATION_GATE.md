# EP-02 — A2/A3 integration gate

Status: **historical design and acceptance baseline**. This gate preceded the opt-in Profile S governor integration included in v5.7.0. See `docs/EP02_GOVERNOR_COMMIT_GATE.md` for the implemented path. This document does not authorize production configuration or broader guarantees.

Baseline: `77594f64bdd7a628445f167419a8fd123addb459`.

Related review stack: PRs #39, #40, #41 and #42.

## Purpose

The experimental EP-02 stack has demonstrated durable request outcomes,
same-host atomic compare-and-set, an authenticated external protocol and
conditional rollback detection. The next decision is how evidence preflight
(A2 in the handoff) may be consumed by the execution lifecycle (A3) without
turning an admissibility finding into execution authority.

This gate defines the contract that must be accepted before any change to the
critical governor, operation registry, budget coordinator or provider path.

## Current boundary

The current components have separate responsibilities:

1. `ep02_signed_trust` authenticates a snapshot and produces a bound preflight
   finding. `ADMISSIBLE` means only that the supplied evidence was acceptable
   under the observed trust snapshot.
2. `ep02_durable_cas` durably advances an anchor checkpoint and records an
   immutable request outcome. It does not authorize an operation.
3. `ep02_external_cas` authenticates remote CAS requests and authority
   receipts. Its receipt proves the authority's response to that request; it
   does not prove that a provider effect or budget settlement occurred.
4. `DualKernelGovernor` registers an operation, reserves budget, claims
   `RESERVED -> DISPATCHING`, calls a provider and records the observed
   outcome. At this historical baseline it had no EP-02 dependency. The published v5.7.0 governor subsequently gained an **opt-in**, disabled-by-default Profile S dependency.

A generic preflight finding or CAS receipt from items 1–3 is not dispatch authorization. In the later opt-in integration, the distinct `CommitAuthorizationReceipt` is used for authorization and single consumption.

## Required decision

Production integration requires a new, explicit artifact:
`CommitAuthorizationReceipt`. It must be issued only after the authority has
revalidated the full operation binding against its current committed trust
checkpoint. It is distinct from both `BoundFinding` and
`SignedExternalReceipt`.

The authorization service may use the durable/external CAS primitives, but
the current generic CAS receipt must not be re-labelled as authorization.

## Immutable operation binding

The authorization request and receipt must bind all fields that can change the
meaning or effect of the operation:

- authority and deployment identity;
- tenant or institutional scope;
- operation and idempotency identifiers;
- passage identifier;
- agent or principal identifier;
- action type;
- resource identifier and canonical amount;
- canonical payload digest;
- evidence claim digest and target-document identifier;
- requested action scope;
- policy identifier and version;
- trust snapshot revision and fingerprint;
- authorization request identifier;
- authority sequence and signing-key generation.

Canonicalization must reject booleans as integers, non-finite amounts,
duplicate fields after normalization, unbounded strings, non-canonical hashes
and unknown fields. A mismatch in any bound field is a rejection without
mutation.

## Integration invariants

**IG-01 — Preflight is non-executing.** A positive preflight finding may be
stored for audit but cannot reserve budget, claim dispatch or call a provider.

**IG-02 — Explicit commit authorization.** Only a verified
`CommitAuthorizationReceipt` bound to the exact operation may satisfy the
trust-commit gate.

**IG-03 — Durable local consumption.** The authorization request ID, receipt,
receipt digest and verification result must be persisted atomically with the
local trust-commit state before dispatch can be claimed.

**IG-04 — Ambiguous authority outcomes do not dispatch.** A timeout or lost
authority response is `UNKNOWN`. Recovery performs lookup with the same
request ID. It never creates a fresh ID, dispatches, releases budget or
compensates automatically.

**IG-05 — One operation, one binding.** Reusing a request or receipt with a
different operation, passage, agent, action, resource, amount, payload,
evidence, policy or trust checkpoint is rejected and audited.

**IG-06 — Quarantine dominates.** A valid authorization cannot bypass an
active quarantine lock. The final quarantine check and local dispatch claim
remain one critical section.

**IG-07 — Authorization is not provider confirmation.** An authorization
receipt proves that dispatch was permitted under a committed trust state. It
does not prove that dispatch occurred or that the external effect succeeded.

**IG-08 — Provider confirmation is not budget settlement.** `CONFIRMED` and
budget commitment remain distinct facts until the durable local transaction
records both according to the budget protocol.

**IG-09 — No unsafe rollback inference.** Local comparison can detect a stale
authority only relative to a preserved independent witness. Joint restoration
of authority and witness remains outside the guarantee.

**IG-10 — Fail closed on unverifiable state.** Missing keys, unavailable
authority, malformed receipts, unknown key generations, stale checkpoints,
signature failure or storage errors block dispatch and produce an auditable
non-success state.

## Proposed local lifecycle

The implementation should introduce explicit trust-commit phases rather than
overloading the existing meanings of `RESERVED` or `DISPATCHING`:

```text
CREATED
  -> governance adjudication
  -> HOLD | FAILED | RESERVED

RESERVED
  -> TRUST_COMMITTING(request_id persisted)
  -> TRUST_UNKNOWN       (authority outcome ambiguous)
  -> TRUST_COMMITTED     (verified receipt persisted)
  -> HOLD                (stale/rejected/unverifiable evidence)

TRUST_COMMITTED
  -> DISPATCHING         (under final quarantine and identity checks)
  -> CONFIRMED | FAILED | UNKNOWN
```

These names are design placeholders. The implementation may use a separate
authorization substate instead of expanding `OperationState`, provided that
the two state machines are persisted atomically and impossible combinations
are rejected.

## Crash and recovery protocol

1. Persist the exact authorization request and request ID before contacting
   the authority.
2. Submit that same request ID until the result can be reconciled.
3. If the call returns an authenticated receipt, verify every binding field
   and authority key generation.
4. Atomically persist the receipt and mark the authorization committed.
5. A crash after authority commit but before local persistence is recovered by
   lookup of the original request ID. Dispatch remains blocked meanwhile.
6. A crash after local persistence but before dispatch resumes from the
   verified receipt and still performs quarantine and operation-identity
   checks.
7. A crash after the local `DISPATCHING` claim follows the existing provider
   reconciliation path and must never resend solely because the local process
   restarted.

## Atomicity boundary

No design can make the remote trust authority, local registry, local budget
store and arbitrary provider effect one ACID transaction. The required safety
property is instead a durable sequence of claims with explicit ambiguous
states:

```text
trust commit -> local receipt persistence -> dispatch claim -> provider call
             -> provider observation -> budget/registry settlement
```

Every arrow is a possible crash boundary. The recovery rule at each boundary
must derive from persisted evidence. Absence of a response is never evidence
that the previous effect did not occur.

## Deployment profiles

### Profile S — single-host

- SQLite authority and operation runtime on one controlled host.
- Same-host process concurrency only.
- Suitable for the bounded mono-host release line after integration tests.
- Does not claim resistance to host or administrator rollback.

### Profile C — central remote authority

- One durable authority serves authenticated clients.
- Requires a production transport, mutual authentication, replay protection,
  key rotation, availability policy and independent high-water witness.
- Network errors produce `UNKNOWN`; no automatic fallback to local approval.

### Profile M — multi-server authority

- Requires a separately reviewed consensus protocol and failure model.
- Not provided by the current experimental stack.
- FM-20 and the Raft production prohibition remain unchanged.

## Required operator decisions

Before runtime implementation begins, the integration owner must select and
document:

1. Profile S or Profile C for the first supported deployment.
2. The authority root, client and receipt-signing key lifecycle.
3. The independent monotone witness and its rollback domain.
4. Whether authorization has a signed logical validity window and which
   trusted time or epoch source evaluates it.
5. Retention and privacy rules for payload digests, claims and receipts.
6. The response policy for prolonged `TRUST_UNKNOWN`.

## Implementation sequence after gate approval

1. Define versioned schemas for authorization request and receipt.
2. Implement a transport-neutral authorization service in `experimental/`.
3. Add durable authorization fields and invariants to a new registry backend
   or transactionally compatible schema.
4. Add a governor integration adapter behind an opt-in feature flag.
5. Connect the adapter immediately before the dispatch claim.
6. Add recovery, audit export and operator tooling.
7. Run the acceptance matrix in
   `EP02_A2_A3_INTEGRATION_ACCEPTANCE_MATRIX.md`.
8. Review the complete diff before enabling any production profile.

## Gate verdict

The EP-02 proof stack is sufficient to design integration. It is not sufficient
to wire the current generic CAS receipt directly into the governor. Runtime
work may start only after the deployment profile and the six operator
decisions above are explicitly accepted.
