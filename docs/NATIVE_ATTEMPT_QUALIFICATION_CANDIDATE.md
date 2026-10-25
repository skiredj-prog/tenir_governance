# Native Attempt Qualification — candidate T6/T7 (opt-in)

**Status:** experimental contract, not production-integrated or REG-certified. Branch PR #65.

## Architectural boundary

`OperationState` is the durable operation/dispatch state machine. `AttemptState` in `operations.attempt_qualification` is a separate, single-host SQLite-bound **evidence qualification record**. It does not dispatch, reserve, compensate, authorize retry, or change any existing governor decision.

- `begin(attempt_id, lei, nonce, deadline_ms)`: records an UNKNOWN attempt; any previously bound LEI is rejected while T8 is unspecified.
- `timeout(attempt_id, observed_ms)`: records timeout knowledge without claiming non-execution, keeping state UNKNOWN.
- `qualify(...)`: asks an independently provisioned verifier to authenticate evidence; accepted `EFFECT_CONFIRMED` closes as RESOLVED; accepted `NON_EXECUTION_CONFIRMED` closes as FAILED.
- Terminal rewrites are rejected. An exact same qualified proof replays idempotently only if its evidence ID, signer, outcome and verification receipt remain identical. A changed proof cannot rewrite terminal history.
- Event records persist locally; not a signed, externally anchored, independently tamper-proof ledger.

## Trust boundary (critical)

`VerifiedEvidence` is **a verified-result type**, not a signature or proof by itself. Security relies on the supplied verifier authenticating signatures, trust roots, anti-replay, expiry, scope, payload/attempt/LEI binding, source authority, and contradiction policies. The module validates shape, attempt/LEI/**nonce** binding, uniqueness of LEI, and terminal monotonicity. A signed attestation authenticates what a trusted authority asserted; it does **not** independently establish that the authority's claim about external execution is factually correct. Therefore no arbitrary callback is sufficient for certification. Production deployment requires an independently audited attestation verifier and durable provenance binding.

To use cryptographically verified evidence, install `tenir-governance[ep02]` and instantiate `Ed25519AttemptVerifier` with trusted Ed25519 public keys, a secured revocation set and a trustworthy clock. It enforces canonical typed signed fields, a bounded validity window, signature integrity, authority allowlisting and revocation. Caller-provided `observed_ms` is **not** the trusted clock and must not be treated as proof of chronology. Generate the attempt nonce with a CSPRNG (at least 128 bits) and bind any external operation/payload context at the authority layer; the prototype does not independently validate a provider's claims.

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
