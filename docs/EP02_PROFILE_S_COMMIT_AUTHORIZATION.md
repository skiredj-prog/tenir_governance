# EP-02 Profile S commit authorization

Status: experimental and detached from the governor.

Baseline parent: `1cbd1275128896a8376022813c0473438bc2b207`.

## Implemented boundary

`ep02_commit_authorization` adds two durable facts to the same SQLite WAL
authority used by `DurableAnchorCAS`:

1. an immutable decision for an operation-bound authorization request;
2. a single immutable consumption result tied to one dispatch claim.

It does not call a provider, reserve or settle budget, change an operation
state, or alter the governor. A caller must still integrate consumption with
the final quarantine check and the local dispatch claim.

## Authorization request

The canonical request binds:

- authority, profile and profile generation;
- institutional scope;
- operation, idempotency, passage and agent identities;
- action, resource and canonical decimal amount;
- payload digest;
- claim digest, prior document and evidence scope;
- policy version;
- trust revision, trust-state digest and signed-snapshot digest.

The request ID is durably bound to this complete envelope. Exact retry returns
the original signed result. Reuse with any changed field raises
`AuthorizationRequestConflict` without mutation.

## Issuance

Before issuing `AUTHORIZED`, the authority:

1. compares the request's trust checkpoint with the current durable anchor;
2. compares the request with the supplied `BoundFinding`;
3. authenticates and revalidates the bound signed snapshot and claim with the
   roots pinned in the authority profile;
4. signs and stores the complete result in the same `BEGIN IMMEDIATE`
   transaction.

A mismatch produces an immutable signed `REJECTED` result. A lost response is
resolved by lookup using the original request ID.

## Consumption

Consumption verifies the receipt, its durable row and the original request.
It then compares the authority's current trust checkpoint with the checkpoint
recorded at issuance.

- exact match and unused receipt: `CONSUMED`;
- changed trust checkpoint: `STALE`;
- rejected authorization: `REJECTED`;
- exact retry with the same dispatch claim: original result;
- another dispatch claim: `AuthorizationConsumptionConflict`.

The consumption record is append-only and signed. It authorizes one local
dispatch claim but does not prove that dispatch or the provider effect occurred.

## Durability evidence

The focused corpus includes:

- all operation-binding substitutions under one request ID;
- trust change before issuance and before consumption;
- tampered and foreign receipts;
- response loss after authorization and consumption commit;
- same-request multiprocess issuance;
- competing multiprocess consumption claims;
- hard process exits after transaction begin, after row insertion, before
  commit and after commit-before-reply for both ledgers;
- immutable-schema guard checks;
- receipt-key pinning;
- canonical amount validation;
- verification that only the payload digest, not payload bytes, is stored.

The tests demonstrate process and SQLite transaction behavior on the tested
filesystems. They do not demonstrate physical power-loss behavior, resistance
to whole-host rollback or atomicity with an external provider.

## Remaining integration work

The next PR may add a governor adapter only behind an opt-in feature flag. It
must persist the authorization request ID before authority contact, persist a
verified authorization result before dispatch eligibility, and combine
successful consumption with the final quarantine and dispatch-claim critical
section. Provider `UNKNOWN` and budget settlement remain governed by their
existing state machines.
