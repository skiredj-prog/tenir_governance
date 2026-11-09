# B1.1-A: retirement of native-only UID dispatch

Status: experimental hardening slice; synthetic effects only. This document
describes the source state introduced by B1.1-A, not production readiness.

## Decision

`ExperimentalPositiveUidService` and its `tenir.positive.uid-lab.v1` socket
protocol are retired. A distinct Linux UID, a native permit and a native claim
do not prove either Governor adjudication or an independent approval. The
service now refuses every request before native admission, I8 reservation,
outbox creation or provider invocation.

`ApprovedPositiveUidService` was already retired by the preceding B1 change.
The only positive UID laboratory path is `DualApprovedUidService`, which
requires both a verified Governor receipt and an independent signed approval.

## Evidence boundary

The unit and disposable-Linux UID tests assert, for the retired native-only
endpoint, zero rows in `reservations` and `w2_positive_v1_outbox`, and zero
synthetic-provider effects. The Linux test runs from a distinct worker UID.
The unit test also has a separate, test-only controlled mutation: restoring a
minimal native-only dispatch makes its independent mock-transport assertion
fail. This shows that the zero-effect oracle is load-bearing; it is not an
application bypass mode and is outside the historical eight-entrypoint W2
mutation inventory.
The tests do not establish protection from a writer-UID compromise, privileged
SQL, older binaries, a different deployed service, or a remote host.

## Relation to B1.1-B

B1.1-A retired only the historical UID socket paths. B1.1-B separately adds a
durable dual-grant check to named native laboratory paths when the store has a
complete B1 trust profile; see [B1.1 native dual grant](B1_1_NATIVE_DUAL_GRANT.md).
It does not change the B1.1-A evidence boundary or establish closure for
generic connectors, older binaries, privileged SQL, other hosts or production.

Generic core Governor and connector APIs, B2 witness/custody, G4-A integration,
multi-host behavior and production readiness are not claimed by this change.
