# EP-02 — Versioned trust and commit binding (draft)

This proposal builds on EP-01 PR #35 without changing the governor.

## Threat boundary

Authentication of a document signature is not authorization of its issuer.
Issuer authority derives from a trusted and independently authenticated
policy snapshot. A positive preflight finding MUST NOT itself execute an action.

## Snapshot contract

- Monotonic trust snapshot revision; canonical hash of keys, mandates,
  revoked issuers, and policy version
- Snapshot-signing trust anchor provisioned out-of-band
- Explicit issuer-to-scope mandates and revocation state
- A receipt linking claim fingerprint(s), old-document ID, action scope,
  policy version and trust snapshot fingerprint
- Any change between evaluation and commit: HOLD for reevaluation, never
  an automatic optimistic commit

## Test plan

EP02-01: unchanged authoritative snapshot and claim -> preflight remains valid.
EP02-02: issuer revoked before commit -> HOLD.
EP02-03: mandate scope withdrawn before commit -> HOLD.
EP02-04: trust revision changes between evaluation and commit -> HOLD.
EP02-05: claim bytes or target document changed -> HOLD.
EP02-06: snapshot provided without authenticated trust anchor -> reject.
EP02-07: caller supplies future/stale evaluation time -> explicit time policy.

## Limits

This is a proposed contract, not a native REG conformant verifier.
The prototype must not be connected to the critical governor or used as
an execution guarantee. A2/A3 remain independent.

## Rotation / rollback characterization added (2026-10-24)

The experimental `ep02_rotation.validate_successor` function verifies a
root-signed candidate against a caller-supplied `TrustCheckpoint`:
- New issuer key requires a strictly newer snapshot revision.
- Same revision with different authenticated snapshot bytes is an
  equivocation error (including same-revision key substitution).
- Old snapshots cannot be replayed against a newer checkpoint.
- Identical snapshot may be reused without changing the checkpoint.

**Still not proven:** durable and rollback-resistant checkpoint storage.
A malicious or stale caller may provide an old checkpoint and replay an
older signed snapshot. Production would require an independently trusted
durable high-water mark (or equivalent consensus authority), atomic
checkpoint advancement, key-rotation authorization policy, recovery after
crash between verifying and persisting, and tests across process/host
boundaries. The experimental function neither writes a checkpoint nor
performs a runtime commit.

A root key itself is out-of-band provisioned. Replacing root keys is
outside the tested issuer-key rotation scenario and must not be inferred
from these tests.
