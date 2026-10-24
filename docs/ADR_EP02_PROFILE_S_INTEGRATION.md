# ADR — EP-02 first integration uses bounded Profile S

Status: proposed for the first A2/A3 integration.

Date: 2026-10-24.

Baseline: `77594f64bdd7a628445f167419a8fd123addb459`.

## Context

The release line already bounds production use to a single host while FM-20
remains open. The EP-02 work now provides tested local durability and a
transport-neutral external protocol, but no production network service or
multi-server authority.

Connecting the external protocol first would add TLS, service availability,
network partition and remote key-operation requirements before the local
authorization semantics are integrated. The first integration therefore uses
the same-host deployment profile.

## Decision

The first A2/A3 integration will implement **Profile S**:

- one controlled host;
- one durable SQLite trust-commit authority;
- multiple local processes may contend through the authority;
- no network listener or remote approval fallback;
- no multi-host, quorum, failover or Byzantine claim;
- FM-20 remains open and production Raft remains disabled.

The integration stays opt-in until its acceptance corpus is green on an exact
candidate SHA.

## Authorization semantics

1. `BoundFinding` remains preflight evidence and cannot authorize dispatch.
2. The local authority issues a versioned `CommitAuthorizationReceipt` bound
   to the complete immutable operation fingerprint.
3. The authority stores the request and immutable result before returning.
4. The local operation registry stores the verified receipt before the
   operation becomes dispatch-eligible.
5. The authorization is single-consumption. Consumption is a CAS against the
   authority's current trust checkpoint and the receipt's unconsumed state.
6. Once consumed, the authorization records the dispatch claim identity. It
   cannot be moved to another operation or reset for retry.
7. Provider execution and reconciliation continue to use the operation's
   existing idempotency key.

The v1 Profile S authorization has no wall-clock expiry. Freshness is expressed
by the authority's monotone sequence and exact trust-checkpoint match at the
linearized consumption step. Adding time expiry later requires a separately
specified trusted-time source.

## Key lifecycle

- The authority receipt key is Ed25519 and provisioned out of band.
- The trusted public key and generation are pinned in deployment
  configuration.
- Private key files must reject symlinks and group/world access on POSIX.
- Rotation creates a new generation; it never overwrites an existing
  generation in place.
- Unknown, retired or ambiguous generations fail closed.
- Root replacement requires an explicit operator ceremony and a new
  deployment manifest. No trust-on-first-use path is permitted.

## Rollback boundary

Profile S does not claim resistance to restoration of all host state by an
administrator or storage operator. A same-host witness can detect some stale
copies but remains inside the same rollback domain.

The runtime and documentation must state this limitation. Independent
rollback resistance requires an out-of-host monotone witness or a reviewed
consensus service and belongs to a later deployment profile.

## Privacy and retention

- Persist canonical digests and required identifiers, not raw payload bytes.
- Avoid copying full evidence documents into the authorization ledger.
- Retain request, result, receipt and consumption records for the same audit
  period as the operation ledger.
- Redaction may remove ancillary metadata but must not change canonical
  fields needed to verify a historical receipt.

## Prolonged ambiguity

`TRUST_UNKNOWN` remains blocked. The system may retry lookup using the original
request ID and raise an operator alert. It must not:

- mint a replacement request ID;
- infer rejection from silence;
- fall back to unsigned or legacy local approval;
- dispatch;
- release budget solely because of the authority timeout;
- create provider compensation.

An operator may restore service or investigate evidence, but cannot declare a
successful authorization without a verified persisted receipt.

## Consequences

This profile can support the bounded mono-host release line after integration
and crash-recovery validation. It deliberately leaves remote authority,
independent rollback resistance and multi-server availability for later work.

The next implementation PR may add versioned authorization schemas and a
transport-neutral Profile S service under `experimental/`. It must not modify
the governor until that service's state machine and failure corpus are green.
