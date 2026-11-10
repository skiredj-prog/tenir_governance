# Integration Contract — 4-Layer State Separation

**Version:** v0.1  
**Date:** 2026-11-10  
**Status:** Draft for lock

## Dimensions (must remain separate)

| Dimension | Field | Allowed values | Rule |
|-----------|-------|----------------|------|
| Permission | `permission_state` | granted \| revoked \| expired | Revocation blocks new admission; does not rewrite outcome |
| Admission / Dispatch | `dispatch_state` | pending \| dispatched \| blocked | One authoritative dispatcher per mission (lease/epoch) |
| Outcome knowledge | `outcome_knowledge` | UNKNOWN \| COMMITTED \| ABORTED | Late receipt may clarify; cannot manufacture failure |
| Mission status | `mission_status` | open \| holding \| closed | Status does not imply outcome knowledge |

## Single-dispatcher lease

Each mission has one lease token. Only the holder may dispatch or consume budget. Human recovery path must acquire the lease; it cannot act as a second concurrent dispatcher.

## Edge cases

1. **Late receipt after bounded human decision**  
   If a correlated receipt arrives after a human decision has been recorded, emit an explicit `reconciliation_conflict` state. Do not silently overwrite UNKNOWN or manufacture clean closure.

2. **Revocation under UNKNOWN**  
   Revoking habilitation while an effect is UNKNOWN immediately blocks further admission/dispatch. Do not rewrite the in-flight outcome to FAILED or ABORTED.

3. **Lease hand-off**  
   Transition from automated to human recovery requires explicit lease transfer. Absence of lease = refusal to dispatch.

## Journal minimum fields

```json
{
  "implementation": "TENIR-Gov",
  "version": "<version>",
  "adapter_version": "<adapter or none>",
  "deployment_scope": "local-single-host-synthetic",
  "permission_state": "...",
  "dispatch_state": "...",
  "outcome_knowledge": "UNKNOWN | COMMITTED | ABORTED",
  "mission_status": "...",
  "lease_epoch": "<token>"
}
```

This contract does not claim runtime equivalence. Adapter version must be recorded in every journal.
