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
