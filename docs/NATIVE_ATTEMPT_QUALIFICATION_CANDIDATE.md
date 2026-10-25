# Native Attempt Qualification — candidate T6/T7 (opt-in)

**Status:** experimental contract, not production-integrated or REG-certified. Branch PR #65.

## Architectural boundary

`OperationState` is the durable operation/dispatch state machine. `AttemptState` in `operations.attempt_qualification` is a separate, single-host SQLite-bound **evidence qualification record**. It does not dispatch, reserve, compensate, authorize retry, or change any existing governor decision.

- `begin(attempt_id, lei, nonce, operation_digest, deadline_ms)`: records an UNKNOWN attempt; any previously bound LEI is rejected while T8 is unspecified.
- `timeout(attempt_id, observed_ms)`: records timeout knowledge without claiming non-execution, keeping state UNKNOWN.
- `qualify(...)`: asks an independently provisioned verifier to authenticate evidence; accepted `EFFECT_CONFIRMED` closes as RESOLVED; accepted `NON_EXECUTION_CONFIRMED` closes as FAILED.
- Terminal rewrites are rejected. An exact same qualified proof replays idempotently only if its evidence ID, signer, outcome and verification receipt remain identical. A changed proof cannot rewrite terminal history.
- Event records persist locally; not a signed, externally anchored, independently tamper-proof ledger.

## Trust boundary (critical)

`VerifiedEvidence` is **a verified-result type**, not a signature or proof by itself. Security relies on the supplied verifier authenticating signatures, trust roots, anti-replay, expiry, scope, payload/attempt/LEI binding, source authority, and contradiction policies. The module validates shape, attempt/LEI/**nonce**/**operation SHA-256 digest** binding, uniqueness of LEI, and terminal monotonicity. A signed attestation authenticates what a trusted authority asserted; it does **not** independently establish that the authority's claim about external execution is factually correct. Therefore no arbitrary callback is sufficient for certification. Production deployment requires an independently audited attestation verifier and durable provenance binding.

To use cryptographically verified evidence, install `tenir-governance[ep02]` and instantiate `Ed25519AttemptVerifier` with trusted Ed25519 public keys, a secured revocation set and a trustworthy clock. It enforces canonical typed signed fields, a bounded validity window, signature integrity, authority allowlisting and revocation. Caller-provided `observed_ms` is **not** the trusted clock and must not be treated as proof of chronology. Generate the attempt nonce with a CSPRNG (at least 128 bits) and supply a canonical, authority-generated SHA-256 digest of the governed operation at `begin()`; the verifier signs the same digest. The prototype does not compute the upstream operation digest or independently validate a provider's claims.

The timeout value is not a proof of failure. No `tau_K` SLA engine or liveness policy is installed by this candidate.

## Tested scope

`pytest tests/test_attempt_qualification_contract.py tests/test_attempt_attestation_ed25519.py tests/test_attempt_qualification_multiprocess.py`: scoped candidate regression and adversarial cases (CI verification needed for current head). Original suite: 9 targeted cases covering qualified positive and negative terminalization, timeout/HOLD semantics, proof rejection, substitution, conflict, delayed qualification, type spoofing, and T8 fail-closed. CI run: https://github.com/skiredj-prog/tenir_governance/actions/runs/40152368180

## Remaining gates

1. Independent security review of Ed25519 verifier, public-key provisioning/revocation and clock integrity; signed fixtures exist but are not a production PKI.
2. Map source operation/dispatch attempt identity and determine atomic or compensating relation to operation registry.
3. Cross-process SQLite qualification tests and atomic event-write rollback are included; operation/attempt *dual-store* crash coupling and physical power loss remain unverified.
4. Establish reviewed `tau_K` operational semantics and qualified evidence acceptance after expiry.
5. Implement a separate RFC-4-conformant adapter in REG and distinguish T6/T7 actual conformance from these candidate unit tests.
6. Keep T8 retry eligibility explicitly out of scope; no merge/release or REG+ claim without separate review.

## Source-to-attempt binding

`operation_digest` is mandatory and syntactically bound as `sha256:<64 lowercase hex>`. It must derive from a **canonical, trusted upstream operation context** (identity, scope, action, resource, payload, policy/version, intended execution site), not from arbitrary caller text. The store detects mismatches but cannot independently prove upstream canonicalization, nor does it coordinate an atomic transaction with the production governor registry. Substitution across otherwise-valid, independently signed operation digests is rejected by test.

## Read-only binding to production operation registry (candidate)

`AttemptOperationObservationBridge` (`tenir_governance.operations.attempt_operation_bridge`) reads an actual **persistent** `DurableOperationRegistry` and produces a canonical SHA-256 binding from stable operation fields: idempotency key, passage, principal, action, amount/resource, payload hash, optional operation/scope IDs, payload digest and commit-gate requirement. It is **not** an external LEI-authority resolver: LEI provenance and the trusted source of these fields must be established upstream. In the attempt's SQLite transaction, `operation_key` is recorded and uniquely indexed, preventing the same source operation from being rebound to a different LEI. The signed attestation contains the same fingerprint.

- `begin_observation` accepts only an UNKNOWN operation from a non-corrupt persistent registry. It **never** claims a dispatch or reserves/settles funds.
- `qualify_observation` checks that the stored operation key/fingerprint and live source remain consistent, records signed evidence only in the attempt ledger, and **rechecks** the source. A mismatch or source-state change produces `HOLD`, with an explicit flag when evidence was recorded despite the race.
- All results have `execution_authorized=false`. Even `EVIDENCE_RECORDED_NO_COMMIT_AUTHORITY` **must not** be treated as authority to alter `OperationState`, compensate a budget or trigger a retry.
- There are **two non-atomically coupled stores**, and the registry can change between reads. A passing check is an observation, **not** a linearizable commit grant or distributed correctness proof. A true execution integration requires an agreed authority/transaction contract and crash-safe recovery protocol; it is explicitly **not** wired here.

Adversarial tests use the real JSON-backed registry and SQLite attempt ledger: terminal drift before qualification, source mutation, corrupt registry, mismatched operation key, unsigned evidence, terminal transitions concurrent with signature verification, and unique source-operation ownership. Tests validate HOLD propagation and non-mutation of the governor registry.

## Governor-facing read-only API and security gate

`inspect_governor_attempt(governor, attempts, operation_key=..., attempt_id=...)` reads the actual `DualKernelGovernor.registry` and returns a structured report with **`decision="HOLD"` and `execution_authorized=false` unconditionally**, including when the attempt ledger has signed `RESOLVED` or `FAILED` evidence. It must not call the provider, adjudication, commit or settlement paths. A nonpersistent registry, unlinked attempt, corrupt source, invalid source state or unmatched operation context yields `candidate_status="HOLD"`. The bridge only accepts an `Ed25519AttemptVerifier` for the live-registry boundary; standalone candidate store tests may use a synthetic verifier but do not imply live integration security.

This API is intentionally observational. Adding authority to modify `OperationState` would require a **separate reviewed atomicity/reconciliation design**, accounting for cross-store crashes, stale evidence, concurrent provider effects and budget compensation. No exactly-once external or multi-host guarantee is asserted.
