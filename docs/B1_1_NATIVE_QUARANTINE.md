# B1.1-C — live native quarantine fence (laboratory)

Experimental single-host profile, stacked on B1.1-B. This extends the named
native admission/send boundary; it does not close B1 system readiness, W4,
B2/custody, privileged-writer attacks or production qualification.

## Authority and scope

The actual Governor receipt issuer and dual-authority service pin one canonical
registry path in the native SQLite store. This pin is immutable through the
supported API. An old B1 store without the pin fails closed and requires review;
opening it is not an automatic migration or authority reassignment.

An old signed receipt is insufficient: the active quarantine marker, its epoch,
and the matching current RESERVED Governor record are checked at each live
native admission, new/pending claim, G4-A grant and send election. Missing or
corrupt registry/marker, a pending release, a different epoch or an unavailable
gate refuses the transaction. A release does not resurrect an older epoch.

## Serialization and recovery

The existing Governor protocol takes the shared file gate before SQLite.
Native transactions take SQLite first, do a **nonblocking** gate check and
release that short check before witness work. Immediately before committing,
they acquire the same gate nonblockingly, reload the durable state, and retain
the lease until COMMIT/ROLLBACK completes. Contention returns HOLD by exception
and rolls back; no waiting cycle with a Governor gate owner is introduced.
Only local claim creation retries gate contention, outside the rolled-back
transaction, with a 500 ms retry window. Every attempt revalidates the same
epoch and identity. Active/corrupt quarantine is not retried; provider calls
are never part of this retry. SQLite lock acquisition has its own timeout,
so 500 ms is not an end-to-end latency guarantee.
Different registry instances for the same canonical path share per-thread
reentrancy. Separate processes still coordinate through the OS file lock.

The commit orders a new admission or send right relative to effective activation
of quarantine. Witness/provider I/O is not inside the final commit lease.
Process death releases the OS lock; SQLite recovers all-or-nothing native writes.
The Governor JSON and SQLite are **not** a common storage transaction: their
supported writers coordinate through this single-host gate. This is neither
distributed fencing nor protection against an old binary ignoring the protocol,
privileged SQL/file edits, deleting lock files, or storage rollback.

An already committed send right remains in flight and may reach a provider
after activation. Quarantine cannot cancel an external effect already authorized.
Read-only claim verification does not elect a new send right; its one-use
provider capability remains required. UNKNOWN/CONFIRMED exact replay and
reconciliation remain possible without obtaining another send right. No key is
rotated to retry an ambiguous effect. Reservations are retained on refusal.

## Acceptance evidence

The test file is `w2_integration/test_b11_native_quarantine.py`. It covers:

- native admission and send refusal, with native state snapshots and an
  independent HTTP MockTransport observer;
- quarantine activation between first validation and COMMIT at admission,
  claim and send, and after the G4-A claim before send election;
- missing/corrupt durable state, missing topology pin and release-pending;
- epoch change after authorized release and immutable registry binding;
- independent processes reaching gate acquisition, rollback under contention,
  and activation ordered after a paused commit;
- a suspended witness while another process activates emergency quarantine;
- abrupt process exits before/after native COMMIT and subsequent recovery;
- UNKNOWN identity replay under quarantine without a new send right.

`scripts/verify_b11_quarantine_mutations.py` requires failures of three specific
safety assertions after test-only removal of admission/send checks or of the
final commit recheck. Setup errors, skips and unrelated failures are rejected.
These fixtures use synthetic providers and test keys; no real effect is tested.

The dedicated workflow runs Linux and Windows at the exact PR head and retains
JUnit, command logs, dependencies and SHA. Its results overlap the full W2
campaign and must not be added to it. The change is a candidate until the final
SHA and its remote runs are recorded in the PR; green CI is not formal proof.
