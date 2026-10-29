# JW-01: bind the actual DispatchProvider arguments

The native adapter previously accepted an approved payload alongside separate
action, amount, agent and metadata arguments. A payload reserving 7 could send
700 and still confirm only 7 against I8. JW-01 makes one complete command the
sole source of the arguments passed to `DispatchProvider.execute()`.

```json
{
  "schema": "tenir-dispatch-command/v1",
  "resource_id": "lab:budget",
  "amount": 7,
  "action_type": "payment",
  "agent_id": "lab-agent",
  "metadata": {
    "recipient": "recipient-1",
    "currency": "EUR"
  }
}
```

All fields are required. The amount is a positive finite JSON number. The
complete command uses the repository's versioned `canon-v1` hash; this change
does not introduce or claim RFC 8785 conformance. Resource, unit, currency and
recipient semantics must be defined by the deployment and connector profile.

Admission snapshots every payload before reading its schema or amount, and
refuses an explicit I8 amount that differs from the payload. Legacy lab
payloads remain admissible but cannot be silently upgraded to executable
commands. They require a new approval for a complete command.

Dispatch snapshots the proposed command, consumes its native permit, reads
the committed outbox payload and checks its hash against the outbox and
permit. Provider arguments come from this fresh stored object. Provider
metadata is detached again so a provider cannot mutate the command used for
the receipt hash. Optional legacy keyword arguments are equality assertions;
any mismatch is rejected before consuming the nonce or calling the provider.

The native adapter can now be called without parallel keyword arguments:

```python
result = adapter.dispatch(operation_key, idempotency_key, command)
```

The JW-01 workflow checks out the PR head explicitly, records its SHA and
executes the command corpus, adapter scenarios and related I8/provider
regressions on Linux and Windows. This is a scoped workflow, not the full
repository or release qualification suite.

## Guarantee boundary and outstanding gates

This change binds the exact arguments passed to `execute()`. It does not
attest to hidden connector configuration or external interpretation. For
example, the existing Stripe connector uses its configured `_currency`;
placing a currency in metadata alone does not bind Stripe's actual currency.
No live Stripe or other external financial API is qualified by these tests.

JW-02 public reservation settlement, pending-dispatch election, review/send
coordination, Witness rollback protection, Witness identity binding, and
expiry-under-lock remain separate open findings. The historical G4-A and
Governor are not wired by this change. Integration Lock, E2E and production
remain HOLD. The archive baseline's known Windows cleanup and stale
cross-boundary test failures are recorded in `JOLT_ARCHIVE_BASELINE.md`.
