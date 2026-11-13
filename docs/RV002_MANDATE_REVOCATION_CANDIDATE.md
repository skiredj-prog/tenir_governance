# RV-002 — Revocation after admission: native dispatch-fence candidate

Status: **DRAFT / UNQUALIFIED / NOT PRODUCTION READY**.

This branch isolates an opt-in change to TENIR-Gov. It must not be merged
or released until the independent RV-002 runners confirm the actual results.

## Finding that motivated the change

RV-002 Runner A showed that native 5.7.0 dispatched one mock provider call
after a mandate-specific revocation was acknowledged. The lab subclass
`GuardedGovernor` prevented it, but was not native and used a process-local
sequencer. The new candidate places the guard at the native claim point.

## Boundary and design

- `SQLiteMandateAuthority` is an **experimental local trusted authority**.
  It does NOT verify signatures, Auth0 JWTs, delegated tokens or the identity
  of the caller. Trusted orchestration must authenticate, register, bind and
  revoke mandates. Production KMS/issuer custody is NOT covered.
- A mandate is created with `mandate_id`, agent identity,
  represented-human identity and bearer-token reference.
- The exact admitted operation ID and canonical SHA-256 payload are bound
  to the active mandate epoch after actual native admission.
- The Governor is configured with `mandate_authority=authority`, and receives
  trusted `authority_context` on dispatch. If configured and missing binding,
  context or a working authority, the dispatch is denied.
- The guard acquires a SQLite `BEGIN IMMEDIATE` transaction and holds it
  across the native registry's `RESERVED → DISPATCHING` transition.
  Revocation uses the same SQLite database and serialized transaction.
  A revocation acknowledged **before** the claim is denied. A claim that
  linearizes **before** revocation is allowed to finish.
- Event sequences are durable SQLite row IDs; this is **not** an independently
  signed witness. The registry and authority are separate databases; a
  process crash during the cross-store interval can produce an ambiguous
  DISPATCHING state requiring reconciliation. No automatic blind replay.
- Default governor instances with `mandate_authority=None` have the original
  5.7.0 behavior. Such unguarded instances MUST NOT be used when a
  mandate-revocation guarantee is claimed.
- This is **pre-dispatch enforcement only**. Target-side revocation after
  gateway acceptance still requires the separate receiving-system lane C.

## Native code path

`DualKernelGovernor.dispatch_and_execute`
→ `dispatch`
→ `_dispatch_under_quarantine_lock`
→ `mandate_authority.claim_fence`
→ registry `transition_to(DISPATCHING)`
→ provider `execute` only after successful claim.

The SDK exposes `mandate_authority` at construction and `payload` plus
`authority_context` for dispatch. The SDK's `adjudicate` also accepts
`payload`, avoiding a mismatch between immutable admission and release.

## Example (synthetic)

```python
from tenir_governance.operations.mandate_authority import SQLiteMandateAuthority
from tenir_governance.operations.payload_binding import canonical_payload_hash
from tenir_governance.sdk import DualKernelCompositionClient

authority = SQLiteMandateAuthority("rv002-authority.sqlite3")
client = DualKernelCompositionClient(
    store_path="rv002-registry.json",
    require_persistence=True,
    mandate_authority=authority,
)
# Trusted identity verification and native admission are required before bind.
# authority.register_mandate(...)
# client.governor.adjudicate(..., payload=immutable_payload)
# authority.bind_operation(operation_id=..., payload_sha256=..., ...)
# For revoked case, trusted authority.revoke_mandate(mandate_id)
# client.governor.dispatch_and_execute(
#     operation_id, payload=immutable_payload, authority_context=trusted_context)
```

## Delegated runner test plan

Use an isolated checkout at this exact branch/commit and record HEAD SHA.
Do not test by substituting `GuardedGovernor`. Do not reuse any previous
5.7.0 test result as evidence that this branch passed.

```bash
python -m unittest tests.test_rv002_mandate_authority -v
python -m unittest tests.test_rv002_native_governor -v
python -m pytest -q tests/test_composition.py tests/test_rv002_mandate_authority.py tests/test_rv002_native_governor.py
```

The last command is a suggested subset: runner must locate the actual
regression suite, record any path differences and run all applicable tests.

### Acceptance criteria (strict)

1. A1 native admission + trusted active mandate → exactly one mock provider
   call and confirmed record; replay/restart never causes a second call.
2. A2 native admission, queue ACK, durable revocation ACK (epoch E2) before
   claim → `DENY_REVOKED`, zero provider calls, and no DISPATCHING transition.
3. A3 unknown/unavailable authority → zero provider calls and explicit denial;
   no conversion of UNKNOWN into FAILED or a fabricated target outcome.
4. A3 actor, human, token, mandate, payload swap → zero provider calls.
5. Concurrent revocation and dispatch across **independent processes**,
   using the SAME durable authority DB → serializable order. A claim before
   ACK may execute; an ACK before claim cannot.
6. Restart with another Governor instance reusing the same authority and
   registry storage → same refusal, no bypass.
7. Alternate dispatch/recovery routes must be enumerated; any route that
   can bypass the optional guard invalidates a deployment-wide claim.
8. Crash injection between registry claim and authority transaction commit:
   no blind retry; ambiguous DISPATCHING reconciled explicitly.
9. Hashes of source SHA, Python versions, raw traces, registry, authority
   events, test logs and relevant provider counters are preserved.
10. Wheel built from the tested commit is independently verified to match
    that source; no credit for a different installation.

### Mandatory non-claims

- Does not imply default TENIR 5.7.0 is fixed.
- Does not prove this lab authority is an authenticated external issuer.
- Does not prove multi-datacenter propagation latency or KMS custody.
- Does not prove receiving-target protected commit (RV-002 C).
- Does not prove production readiness or full end-to-end non-bypassability.

## Report handoff

Return `PASS / FAIL / INCONCLUSIVE / NOT_RUN` for each case, raw
evidence and SHA-256 manifest. Source/PR being available is **not** a PASS.
Keep Integration Lock, E2E and Production at HOLD until independent evidence.
