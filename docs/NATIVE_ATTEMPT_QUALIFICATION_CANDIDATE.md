# Native Attempt Qualification — candidate T6/T7 (opt-in)

**Status:** experimental contract, not production-integrated or REG-certified. Branch PR #65.

## Architectural boundary

`OperationState` is the durable operation/dispatch state machine. `AttemptState` in `operations.attempt_qualification` is a separate, single-host SQLite-bound **evidence qualification record**. It does not dispatch, reserve, compensate, authorize retry, or change any existing governor decision.

- `begin(attempt_id, lei, deadline_ms)`: records an UNKNOWN attempt; any previously bound LEI is rejected while T8 is unspecified.
- `timeout(attempt_id, observed_ms)`: records timeout knowledge without claiming non-execution, keeping state UNKNOWN.
- `qualify(...)`: asks an independently provisioned verifier to authenticate evidence; accepted `EFFECT_CONFIRMED` closes as RESOLVED; accepted `NON_EXECUTION_CONFIRMED` closes as FAILED.
- Terminal rewrites are rejected. An exact same qualified proof replays idempotently.
- Event records persist locally; not a signed, externally anchored, independently tamper-proof ledger.

## Trust boundary (critical)

`VerifiedEvidence` is **a verified-result type**, not a signature or proof by itself. Security relies on the supplied verifier authenticating signatures, trust roots, anti-replay, expiry, scope, payload/attempt/LEI binding, source authority, and contradiction policies. The module validates only shape, attempt/LEI and terminal monotonicity. Therefore no arbitrary callback is sufficient for certification. Production deployment requires an independently audited attestation verifier and durable provenance binding.

The timeout value is not a proof of failure. No `tau_K` SLA engine or liveness policy is installed by this candidate.

## Tested scope

`pytest tests/test_attempt_qualification_contract.py`: 9 targeted cases covering qualified positive and negative terminalization, timeout/HOLD semantics, proof rejection, substitution, conflict, delayed qualification, type spoofing, and T8 fail-closed. CI run: https://github.com/skiredj-prog/tenir_governance/actions/runs/40152368180

## Remaining gates

1. Security-review verifier contract with real signature fixture, revocation, freshness and independent trust roots.
2. Map source operation/dispatch attempt identity and determine atomic or compensating relation to operation registry.
3. Crash/process adversarial tests of evidence/operation dual-write and restart; check cross-process races.
4. Establish reviewed `tau_K` operational semantics and qualified evidence acceptance after expiry.
5. Implement a separate RFC-4-conformant adapter in REG and distinguish T6/T7 actual conformance from these candidate unit tests.
6. Keep T8 retry eligibility explicitly out of scope; no merge/release or REG+ claim without separate review.
