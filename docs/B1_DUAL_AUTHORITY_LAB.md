# B1: Governor AND independent approval laboratory

Status: experimental, synthetic effects only. **B1 system readiness and
production remain HOLD.** This is source-only code under `w2_integration`, not
an exported wheel connector. The selected targets are generic HTTP and Stripe
on one Linux host. Neither real provider is connected by this change.

## Authorization contract

`GovernorReceiptIssuer` verifies an independently signed exact command, invokes
an actual `DualKernelGovernor` with operator-supplied scoring context, checks
its persistent RESERVED record and I2 payload binding, and signs a receipt
with a different Ed25519 key. It does not accept a caller-supplied verdict.
The default allowed decision is PASS. FLAG requires explicit configuration in
both issuer and service; the test fixture deliberately configures this and
does not equate FLAG with PASS. HOLD, BLOCK and other decisions cannot authorize
the service. Distinct keys alone do not establish organizational independence.

`DualApprovedUidService` requires that receipt AND the independent approval.
It pins their public keys and the allowed Governor decisions. The receipt
binds the entire signed request, operation and evaluation identities, passage,
expiry, quarantine epoch, and a hash of the configured scoring inputs. That
hash is **not** a measurement of the deployed policy code or binary.

The issuer persists its receipt and returns that same receipt on an exact
replay; it does not adjudicate again. A conflicting request, missing/corrupt
registry, changed trust, expired approval, changed epoch or invalid signature
fails closed. Native nonce, approval binding, I8 reservation and permit retain
their existing transactional checks.

## Shared quarantine and send decision

The issuer, service and emergency controller must use the SAME persistent
Governor registry in the writer's private state directory. Admission and
durable send-right issuance acquire its cross-process quarantine gate before
checking quarantine and the bound RESERVED record. Lock ordering is:

1. Governor quarantine gate.
2. A short registry read (its file lock is released on return).
3. Native SQLite write transaction.

The send transaction rechecks expiry and pinned trust after acquiring the
SQLite writer lock. A lock/unlock cycle invalidates earlier Governor receipts;
a cycle during adjudication also prevents issuance of a new receipt for that
decision. Failure after a native claim but before send leaves the reservation
and PENDING outbox in place without a send right.

The quarantine gate is released before provider I/O. An operation granted a
send right before quarantine activation may still produce its already
authorized synthetic effect, including if the call starts just after lock
activation. Quarantine does not cancel an effect already authorized. A blocked
provider call does not hold the gate. Local SQLite/file-lock delays can still
delay activation; this is not a bounded emergency-response SLA.

This live Governor JSON-quarantine gate applies to the guarded
`DualApprovedUidService` and HTTP laboratory path. Direct native B1-profile
enforcement records and validates a signed receipt epoch and pinned trust, but
does not make the JSON registry and SQLite I8 state one transaction or provide
a native monotonic quarantine fence.

## Crash and trust boundaries

The Governor JSON registry, its in-memory budget and the native SQLite I8
store remain distinct. There is no transaction spanning those components.
If adjudication persists but receipt persistence fails, replay refuses the
orphaned Governor record and requires recovery. Tests inject a transaction
failure and terminate a separate process with `os._exit` immediately before
and after the receipt commit. After-commit replay returns the persisted receipt
without adjudicating again. These are not power-loss durability proofs.
Native send-right/crash tests remain
separate evidence and their counts must not be added to overlapping suites.

There is no automatic budget release, retry of an uncertain effect, or
positive external-effect confirmation here. Synthetic receipts remain
`execution_authorized=false`. B2/anti-rollback, real provider reconciliation,
protected key custody and independent integrated W4 evidence are not closed.

The Linux test controller creates separate non-root writer and worker UIDs
and installs read-only source. Workers receive public material only. The
issuer has a laboratory private key; the service has other laboratory signing
keys. Environment injection of test keys is not a production secret manager.
A compromised writer UID, signer, host administrator or privileged SQL actor
is outside this lab's protection. Multiple hosts are unsupported.

## Legacy paths and interpretation of CI

The older `ApprovedPositiveUidService` and
`ExperimentalPositiveUidService` socket endpoints are retired fail-closed;
they cannot execute from an independent approval or a native-only UID request.
The signed-endpoint regressions assert zero approval bindings, I8 reservations
and outbox rows; the native-only endpoint regressions assert zero provider
effects, I8 reservations and outbox rows. The cooperative Governor proxy and
historical `StripeProvider.execute()` remain separate interfaces and are not
upgraded or certified by this experiment. Green CI is not B1 system acceptance.

The W2 migration now preserves the historical assertions while refusing
unclaimed execution and closing the owned SQLite handles that previously failed
on Windows. These results do not authorize a deployment to treat any remaining
legacy or generic connector as equivalent to the dual-authority service, nor
to place real provider credentials in workers.

The new suite covers missing/forged proofs, pinned context, exact replay,
receipt append-only behavior, expiry before send, quarantine between claim
and send in separate processes, quarantine epoch changes, damaged registry,
receipt persistence failure, and a suspended synthetic provider. Dedicated
Linux tests send directly to the service from a different UID. Windows skips
the Linux-only UID campaign. The workflow checks out and records the exact
PR head SHA; synthetic merge results are not substituted for that evidence.

From this checkout, with Python 3.12 and dependencies installed:

```bash
python -m pip install -e '.[api,test,raft,ep02]'
python -m pip check
PYTHONPATH="$PWD:$PWD/w2_integration" TENIR_ENV=test HOST_SCOPE=single-host \
  python -m pytest -q -rx -o addopts= w2_integration/test_b1_dual_authority.py
```

Use the dedicated CI job for the root-controlled disposable UID campaign;
do not run it on a production host. Record PASS, FAIL, SKIP and XFAIL separately.

## Remaining readiness gates for both chosen providers

| Gate | Measurable closure condition |
| --- | --- |
| HTTP/Stripe provider contract | Provider-specific command, identity, timeout, status and reconciliation semantics tested; no synthetic receipt represented as a real effect. |
| Mandatory deployment authority | One supported entry point requiring both approvals; worker cannot invoke an older service, read a provider secret or invoke provider credentials directly. Inventory old writers and binaries. |
| Atomic ownership and recovery | One documented budget authority or a reviewed recovery protocol with process-kill tests at every cross-store boundary; no second effect or automatic release inferred from timeout. |
| Custody and deployment | Trusted issuer configuration, protected key service, distinct identities, secret access and network policy verified on the actual deployment. No secrets needed in source or reports. |
| B2 and W4 | Independent monotone witness and a single integrated SHA/topology/CEB-01 subjected to the agreed adversarial campaign. |

GO for reviewing this laboratory requires its new regression tests and distinct
UID campaign to pass on the same SHA. That decision is separate from B1 system
readiness, release qualification, merge or publication authorization.
