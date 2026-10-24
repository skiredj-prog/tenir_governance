# EP-02 — A2/A3 integration acceptance matrix

Status: implemented for Profile S on `integration/v5.7-ep02-acceptance`;
exact-SHA remote validation remains required.

Baseline: `77594f64bdd7a628445f167419a8fd123addb459`.

The matrix defines observable properties for the future integration. It does
not claim that the current governor implements them.

| ID | Scenario | Required observation |
| --- | --- | --- |
| INT-01 | Valid preflight, no commit authorization | No dispatch claim; no provider call |
| INT-02 | Exact valid commit receipt | Receipt persisted before dispatch becomes eligible |
| INT-03 | Receipt used with another operation ID | Reject without mutation |
| INT-04 | Passage, agent or action substituted | Reject without mutation |
| INT-05 | Resource or canonical amount substituted | Reject without budget mutation |
| INT-06 | Payload bytes differ from bound digest | Reject without provider call |
| INT-07 | Claim, target document, scope or policy substituted | Reject without mutation |
| INT-08 | Trust revision/fingerprint stale at commit | HOLD; no dispatch |
| INT-09 | Receipt signature or key generation invalid | HOLD; security event recorded |
| INT-10 | Unknown or retired authority root | HOLD; no compatibility fallback |
| INT-11 | Authority returns APPLIED, response is lost | Original request lookup recovers one result |
| INT-12 | Authority request times out before known result | `TRUST_UNKNOWN`; no fresh request ID |
| INT-13 | Retry same ID with identical request | Original immutable result returned |
| INT-14 | Retry same ID with changed request | Request-ID conflict; no mutation |
| INT-15 | Crash before authority call | Recover pending request; no dispatch |
| INT-16 | Crash after authority commit before local receipt | Lookup and persist receipt; no duplicate commit |
| INT-17 | Crash after local receipt before dispatch claim | Reverify receipt and quarantine before claim |
| INT-18 | Crash after dispatch claim before provider response | Reconcile provider; do not resend automatically |
| INT-19 | Quarantine activates after authorization | Dispatch blocked under shared quarantine lock |
| INT-20 | Budget reservation no longer valid | Dispatch blocked; receipt does not override I8 |
| INT-21 | Provider CONFIRMED, settlement write crashes | Recover explicit settlement-pending state |
| INT-22 | Provider outcome ambiguous | Operation remains `UNKNOWN`; no release or compensation |
| INT-23 | Independent witness observes older authority | Reject stale authority and alert |
| INT-24 | Authority and witness restored together | Test reproduces non-detection and documents limitation |
| INT-25 | Malformed/unbounded canonical input | Reject before signature or state mutation |
| INT-26 | Receipt replay after terminal completion | Return existing result; no new dispatch |
| INT-27 | Two workers consume same receipt | At most one obtains the dispatch claim |
| INT-28 | Two hosts submit competing authorization writes | Central authority produces one monotone winner |
| INT-29 | Authority unavailable during recovery | Remain blocked and auditable; no local approval fallback |
| INT-30 | Legacy v1 issuer-key path attempted by default | Reject unless explicit compatibility mode is enabled |

## Required test layers

1. Pure schema and canonicalization tests.
2. Local transactional tests with injected exceptions at every write boundary.
3. Real multiprocess races and hard process exits.
4. Provider timeout and reconciliation tests.
5. Network fault tests for the central authority profile.
6. Backup/restore tests with the witness inside and outside the rollback domain.
7. Packaging tests proving that optional integration dependencies are declared.
8. Exact-head CI with counts and skips recorded.

## Stop conditions

Integration is a release blocker if any test demonstrates:

- provider execution from preflight alone;
- dispatch after a receipt-binding mismatch;
- automatic retry with a fresh authorization or provider request ID after an
  ambiguous result;
- budget release or compensation based only on timeout;
- bypass of quarantine by a previously authorized operation;
- silent fallback from an unavailable external authority to local approval;
- a claim of multi-host or rollback safety broader than the tested deployment
  profile.
