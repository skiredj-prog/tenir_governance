# B1 HTTP + Stripe contract qualification — local mocks

This source-only laboratory implements the owner's HTTP and PaymentIntent test
contracts. It requires `TENIR_ENV=test` or `lab` and `HOST_SCOPE=single-host`.
It refuses real HTTP transports and has no Stripe credential parameter.
Receipts and responses explicitly say `synthetic=true` and
`execution_authorized=false`. **This is not a deployed payment gateway or B1
system/production readiness.** The historical G4-A verdict is unchanged.

## Worker contract

`POST /api/v1/effects/execute` accepts exactly:

```json
{
  "operation_id": "UUIDv4 generated during Governor preparation",
  "idempotency_key": "SHA-256 of the canonical effect intent",
  "action_type": "stripe_charge",
  "authority_context": {"grant_id": "durable grant UUID", "signature": "base64 Ed25519 signature"},
  "payload": {"amount": 120.00, "currency": "EUR", "destination": "pm_mock"}
}
```

The strings above describe fields, not usable credentials. `prepare()` runs
on the trusted Governor side and creates the operation ID and an unsigned
independent-approval request. An independent signer signs that request;
`approve()` verifies it and invokes the actual `DualKernelGovernor`. Only PASS
can produce the HTTP grant; FLAG, HOLD and veto cannot. The grant binds the
entire intent plus the persisted Governor receipt and independent approval.
These trusted preparation methods are not exposed as worker HTTP endpoints.

The idempotency hash covers UTF-8, sorted-key, compact JSON containing
`action_type` and `payload`. Amounts are positive, limited to two decimal places
and normalized so `120` and `120.00` have the same intent identity. This lab
supports EUR, USD and GBP, up to 999999.99 units. The native command separately
binds the configured agent and budget resource. The public intent hash, native
operation UUID and native execution ID serve different purposes and are
persistently linked; they are not interchangeable.

A valid POST returns **202** and the operation ID after native I8 reservation,
independent approval/nonce binding and the HTTP outbox row commit together.
It does not call the provider in the HTTP request. A trusted dispatcher runs
`run_once(operation_id)` separately. Invalid authority is 403, an identity or
payload conflict is 409, and malformed JSON/unsupported input is rejected.
Duplicate JSON keys are rejected.

`GET /api/v1/effects/status/{operation_id}` returns PENDING, CONFIRMED with a
receipt, FAILED with an error code, or UNKNOWN. The read requires the same
grant in `X-Tenir-Grant-Id` and `X-Tenir-Grant-Signature` headers. It performs no
provider call. Repeating a valid POST returns cached state, including UNKNOWN
and after approval expiry, without dispatching or scheduling a retry. A new
admission or send still requires unexpired dual authorization.

## Send and outcome rules

Before sending, the service revalidates the Governor receipt, independent
approval, record and shared quarantine; it consumes the native claim and
atomically records a one-time send election with the HTTP outbox's UNKNOWN
state. Provider I/O occurs after that commit and outside the quarantine gate.
Repeated calls, concurrent threads/processes and process restart cannot elect
a second send. Death before the send commit rolls it back; death after it
leaves UNKNOWN, even if no request reached the provider. Availability is
deliberately sacrificed in that ambiguity window.

The native permit/claim/witness stages are still separate transactions from
Governor adjudication. Governor JSON state and its memory budget are not
atomic with the native SQLite authority. Neither a shared lock nor repair
turns them into one distributed transaction.

- Generic HTTP uses the configured mock destination, the requested execution
  endpoint and the intent hash as `Idempotency-Key`. Correlated 202 is PENDING;
  correlated success requires a receipt. Positive confirmation also performs
  a read-only status check before native I8 commit.
- Stripe creation uses `POST /v1/payment_intents`, `confirm=true`, integer
  minor units, lowercase currency, a configured `pm_` payment method, and the
  operation UUID as `Idempotency-Key`. Metadata binds operation ID and intent
  hash. A configured existing `pi_` is read and checked for the same amount,
  currency and metadata before a single `/confirm` POST. A mismatched or
  unbound existing intent is not confirmed.
- Timeouts remain UNKNOWN. No POST retry and no automatic reconciliation is
  triggered by the timeout, a GET, or a repeated public POST. The explicit
  `reconcile_once()` job only reads provider state.
- Stripe reconciliation retrieves a known PaymentIntent ID. If it was lost
  with the response, it searches the operation metadata, requires one matching
  result and then retrieves that ID. Empty, partial or multiple results remain
  UNKNOWN. There is no fictitious GET-by-idempotency-key endpoint.
- Only a correlated `succeeded` result with matching amount received, currency,
  payment method/intent, metadata and `livemode=false` supports a synthetic
  Stripe confirmation. Processing/action-required results remain pending.
- A bare HTTP 400 does **not** establish non-execution. The local rejection
  fixture supplies an explicit correlated `no_effect=true` observation. Only
  that fixture returns FAILED with code FAILED_PROVEN. This additional fixture
  field is not represented as a Stripe API guarantee.
- FAILED retains the budget and creates a native review latch. Contradictory
  terminal observations become UNKNOWN with `review_required=true`; both
  observations are kept in an append-only table. A late positive observation
  cannot automatically override a prior proven failure. No automatic release
  or compensation is inferred.

The confirmation receipt is signed by the **connector attestor**, anchored in
the native authority. It is not a Stripe signature, an independent witness or
a certificate of the underlying real-world effect.

Stripe's primary references support [create-and-confirm](https://docs.stripe.com/api/payment_intents/create),
[retrieval by ID](https://docs.stripe.com/api/payment_intents/retrieve),
[metadata search and its consistency limits](https://docs.stripe.com/api/payment_intents/search),
and [API v1 idempotency behavior and retention limits](https://docs.stripe.com/api/idempotent_requests).
They do not justify claiming exactly-once external effects. Local persistent
send election is stricter than a temporary provider-side idempotency cache.

## Reproduce and interpret results

From this source checkout:

```bash
python -m pip install -e '.[api,test,ep02]'
PYTHONPATH="$PWD:$PWD/w2_integration" TENIR_ENV=test HOST_SCOPE=single-host \
  python -m pytest -q -o addopts= w2_integration/test_b1_effect_contracts.py
```

The tests use actual FastAPI requests against an in-process ASGI application
and `httpx.MockTransport` for both providers. Test-only measured coordinates
exercise TenirKernel and the actual Governor PASS path; they are not a
production risk model. The unchanged default adapter's FLAG is explicitly
tested as insufficient. Separate processes exercise send election and abrupt
exit before commit, before I/O, and after a synthetic effect. No real payment,
remote service, API secret or live network provider is involved.

On the restricted Windows sandbox, asyncio's internal socketpair can block
before TestClient setup. Use a permitted local test environment or Linux CI;
that setup block is not a test pass. The repository's old W2 tests and old
signed-service XFAIL remain visible in the broad workflow. Their failures
prevent declaring the entire branch ready even if this contract suite passes.

Next deployment gates remain: close legacy paths in one supported deployment,
review the budget/recovery authority, protect credentials and signer custody,
verify real provider semantics in an authorized non-production environment,
and complete B2/W4 at one integrated SHA/topology. See
[dual-authority scope](B1_DUAL_AUTHORITY_LAB.md). No merge or publication is
implied by the mock contract qualification.
