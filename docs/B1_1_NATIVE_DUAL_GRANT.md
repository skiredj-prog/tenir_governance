# B1.1-B — native dual-grant boundary (laboratory)

Status: experimental, single-host laboratory only. This source slice does not
close B1 system readiness, B2, G4-A/W4 integration or production deployment.

## Narrow contract

When, and only when, the SQLite store contains the complete B1 trust profile,
native admission requires a durable dual grant derived from a signed Governor
receipt and a separately signed approval. A missing, malformed, expired or
mismatched proof fails before creation of a native I8 reservation, permit or
outbox claim.

The native authority stores the verified proof append-only in
`b1_native_dual_grants`. Admission then revalidates it inside the writer
transaction and commits the approval binding, native permit, I8 reservation
and permit link together. Proof storage and admission are distinct
transactions: a crash between them leaves an unused proof record, not a
dispatch authorization.

The proof keeps both identities deliberately separate:

- the canonical command hash binds approval, permit, reservation and payload;
- the full signed-request hash binds the Governor receipt.

It also binds operation, evaluation, idempotency identity, key identifiers,
policy-context hash, audience, expiry and the receipt's recorded quarantine
epoch. An existing permit without the exact durable permit link is refused for
review; a later proof cannot retroactively adopt it.

## Named implementation boundary

For a complete B1 trust profile, the following named native laboratory paths
consult the durable proof:

- `NativePositivePermitAuthority.admit_reserve_issue()` before I8 admission;
- `NativePositivePermitAuthority.consume_and_claim()` before creating or
  replaying a dispatch claim;
- `NativePositivePermitAuthority.grant_send_right()` and
  `verify_dispatch_claim()` before a send capability is used;
- `NativePositiveG4A.evaluate_and_grant()` through native admission; and
- `NativePositiveG4A.dispatch_to_b3()` again in its send-election transaction.

The `KernelClaimProofProvider` and `NativeClaimedDispatchAdapter` reach the
same native claim/send checks. The tests name these entries individually. This
does **not** establish that every historical, generic, deployed or privileged
connector entry point is closed.

## Evidence and deliberate limits

The tests use synthetic providers and test keys. Rejection at admission or
claim leaves native I8, permit and outbox state unchanged. Rejection at the
later G4-A send check retains the already-created PENDING claim and reservation
without consuming the grant or recording a send election. Provider observers
assert zero MockTransport requests at these tested boundaries. The B3 observer
is injected at synthetic execution entry and is independent of its ledger.
A test-only mutation that removes the admission check must make the native
state oracle fail; it is not an application bypass setting.

The proof links directly to permit and reservation. Its association with the
G4-A grant and execution/outbox is transitive through the existing permit and
operation identities. It does not introduce a new signed provider-receipt
schema. Commit-failure injection covers proof storage, admission and claim;
the existing HTTP campaign covers process exits and concurrent dispatch.

The B1.1-B baseline leaves the Governor registry in a separately locked JSON store. A direct native
B1-profile call validates the signed receipt epoch and pinned trust, but it
does not atomically re-read a live Governor quarantine state with the SQLite
I8 transaction. It is therefore not a native monotonic quarantine fence.
An earlier valid dual proof alone does not establish current admissibility
after a quarantine change. This remains a P0 for promoting direct native
dispatch to a supported deployment; B1.1-B must remain Draft until that next
fence and the integrated tests have been reviewed. The subsequent candidate
[B1.1-C](B1_1_NATIVE_QUARANTINE.md) adds a shared single-host commit gate; its
evidence must be reviewed at its own SHA. It does not retroactively strengthen
the B1.1-B baseline or create a joint JSON/SQLite storage transaction.

The following remain open: B2/witness and protected key custody; the
historical G4-A/W4 integration boundary; generic core and connector APIs;
older binaries and privileged SQL; cross-store atomicity and live quarantine
fencing; multi-process completeness; multi-host behavior; real-provider
credentials and production deployment.

Each result is evidence only for the exact commit, command, operating system
and paths recorded with it. A green unit or CI job does not prove a real
provider effect, credential custody, writer isolation, multi-host behavior or
deployment readiness.
