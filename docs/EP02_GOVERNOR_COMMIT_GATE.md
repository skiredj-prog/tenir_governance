# EP-02 Profile S Governor commit gate

## Status and scope

This integration is opt-in and disabled by default. It connects the Profile S
commit authority to `DualKernelGovernor` and the durable operation registry for
single-host deployments. Existing Governor callers retain their previous
dispatch path unless `require_commit_authorization=True` is configured.

Profile S covers multiple processes on one controlled host through SQLite WAL
and host-local file locking. It does not claim provider atomicity, inter-host
consensus, or external anti-rollback. FM-20 remains open and production
multi-host Raft remains outside this profile.

## Configuration

Construct the Governor with both options:

```python
governor = DualKernelGovernor(
    scoring,
    membrane,
    registry=registry,
    commit_authority=profile_s_authority,
    require_commit_authorization=True,
)
```

When the gate is enabled, `adjudicate()` also requires:

- `payload_digest`: lowercase SHA-256 of the exact provider payload;
- `operation_id`: stable logical operation identity, defaulting to the
  idempotency key;
- `institutional_scope`: bounded deployment scope.

An accepted adjudication reaches `RESERVED`. It is not yet dispatchable. The
caller must build a `CommitAuthorizationRequest` whose complete
`OperationBinding` matches the durable operation, then call:

```python
decision = governor.authorize_commit(
    idempotency_key,
    request,
    finding=finding,
    claim=signed_claim,
    current_snapshot=signed_trust_snapshot,
    now=decision_time,
)
```

Only a signed `AUTHORIZED` receipt permits the later dispatch path to attempt
single consumption.

## Dispatch protocol

1. The Governor persists the canonical request before contacting the authority.
2. It persists and verifies the signed authorization receipt.
3. `dispatch()` checks quarantine briefly, then releases the quarantine lock.
4. The registry persists one deterministic dispatch claim identity.
5. The authority consumes the authorization in SQLite. A lost response is
   recovered through lookup with the same request and claim identities.
6. The registry persists the signed consumption receipt.
7. Under the quarantine lock, the Governor revalidates the complete operation
   binding and both signatures.
8. Only then may the registry claim `RESERVED -> DISPATCHING`.

Authority calls never run while the quarantine lock is held. If quarantine
activates after consumption and before the state claim, dispatch remains
blocked in `RESERVED`. The consumed receipt can be replayed after an authorized
quarantine release; it does not imply that a provider call occurred.

## Fail-closed recovery

- Lost authorization reply: lookup the durable authorization.
- Lost consumption reply: lookup the durable consumption.
- Unknown authority outcome: remain `RESERVED`; do not call the provider.
- Trust checkpoint changed before consumption: record `STALE`; do not dispatch.
- Rejected or invalid receipt: remain `RESERVED`; do not dispatch.
- Registry persistence failure: do not dispatch.
- Corrupt or substituted binding: do not dispatch.
- Process crash after consumption: replay the same deterministic claim and
  continue only after the signed receipt is durably attached to the operation.

The provider outcome state machine remains unchanged after `DISPATCHING`:
timeouts and ambiguous results still produce `UNKNOWN`, and reconciliation
must not blindly resend an external effect.

## Evidence

`tests/test_ep02_governor_commit_gate.py` covers opt-in compatibility, complete
identity persistence, missing authorization, substitution, registry bypass,
lost replies, stale trust, quarantine races, receipt tampering, recovery and
two-worker claim behavior. The existing Profile S authority suite remains the
source of SQLite crash and multiprocess authorization evidence.
