# EP-02 durable request outcomes

Status: experimental, opt in, single host. This API is detached from the
governor and does not authorize irreversible execution.

`DurableAnchorCAS` stores the trust checkpoint and an immutable terminal
request outcome in one SQLite transaction. A client supplies a stable request
ID and retries that same ID with the exact same request after an uncertain
reply.

Install the experimental cryptographic dependency with:

```console
pip install 'tenir-governance[ep02]'
```

## Provision and use

Provisioning creates a new authority database exactly once. Ordinary open does
not create a missing database.

```python
from tenir_governance.experimental.ep02_durable_cas import DurableAnchorCAS

store = DurableAnchorCAS.provision(
    "trust-authority.sqlite3",
    authority_id="authority-a",
    trust_profile_id="institutional-roots",
    trust_generation=1,
    roots={"root-2026": root_public_key},
)

bootstrap = store.bootstrap("request-bootstrap-0001", signed_initial_snapshot)
expected = store.get()
result = store.compare_and_swap(
    "request-rotation-0001", expected, signed_successor_snapshot,
)
```

The request digest binds the operation, authority, root profile generation,
expected checkpoint, and exact signed successor envelope. Reusing an ID with a
different digest raises `RequestIdConflict` and does not mutate the database.
An exact retry returns the original result and marks it `replayed=True`; its
`current_checkpoint` is a separately labelled current observation.

## Resolving an uncertain reply

An exception or lost reply leaves the caller's knowledge `UNKNOWN`. It is not a
stored terminal result and must not trigger compensation or a new request ID.

```python
digest = store.cas_request_digest(expected, signed_successor_snapshot)
observation = DurableAnchorCAS.open(path).lookup(request_id, digest)
```

| Observation | Meaning | Permitted next action |
| --- | --- | --- |
| `RECORDED` | The transaction committed. | Use the original recorded outcome. |
| `NOT_RECORDED` | No committed row was visible in that read snapshot. A live writer may still commit. | Retry the exact same ID and context. |
| `RequestIdConflict` | The ID is bound to different context. | Fail closed and preserve the first record. |
| Storage/integrity exception | The authority cannot establish an outcome. | Keep the outcome unknown and stop authoritative use. |

SQLite uses WAL, `synchronous=FULL`, a writer transaction, and per operation
connections. The authority row and request outcomes have database guards
against update and deletion. Reopen validates the schema, root profile,
canonical envelopes, signatures, request digests, outcome derivation, and the
checkpoint's complete state changing history. The unconditional legacy
`advance()` path is rejected.

## Tested scope and limits

The tests cover exact retry after restart, later commits, conflict retention,
context substitution, concurrent processes, competing bootstrap, live writer
lookup, lock failure, WAL checkpoint/reopen, database replacement, and real
process exits before and after each transaction boundary.

These properties apply to one trusted local filesystem and SQLite authority.
They do not establish:

- inter-host consensus or distributed linearizability;
- atomicity with a provider, dispatch, budget store, or irreversible effect;
- protection from an administrator replacing the database, WAL files, root
  profile, and all local evidence with a consistent older copy;
- physical power-loss guarantees beyond SQLite's documented guarantees for the
  actual filesystem and hardware;
- production or institutional assurance.

Authenticated reads are not atomic writes. A separate external monotone CAS
authority and independent anti-rollback evidence remain required for cross-host
or hostile-local-storage claims.
