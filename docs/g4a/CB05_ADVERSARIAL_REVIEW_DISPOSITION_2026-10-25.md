# CB-05 adversarial review disposition — 2026-10-25
Status: **DESIGN REVIEW / NOT IMPLEMENTED / NOT DEMONSTRATED**

## Provenance
Reviewer reports second execution on a fresh clone of SUT SHA `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`, with separately rewritten harness, but **same operator** as original static review and initial execution. Raw evidence at `/home/claude/r02/indep/` was not attached or independently inspected. Reported Q6 inactive quarantine mark rollback admits, Q1 deletion of both mark/sentinel admits, Q1 mark-only deletion HOLD, P6 post-commit OSError at JSON and gate lock close yields caller HOLD with durable commit and no duplicate on replay. Record as **REPORTED / SAME-OPERATOR REPRODUCTION**, not independent confirmation.

## Disposition of six CB-05 objections
1. **Quarantine omitted from event-chain-only anchor — ACCEPT.** Checkpoint MUST bind quarantine generation, current mark, sentinel/pending state and release evidence; each admission MUST be validated against a fresh authorized quarantine head. An anchored digest only at periodic checkpoints leaves a race/window: define an explicit linearization boundary or HOLD while transitions are unanchored.
2. **Witness on same UID/disk not independent — ACCEPT.** G1 OS isolation and distinct durable witness identity/trust root are prerequisites for claims against privileged local writers. A local file under same credentials does not count.
3. **Previous checkpoint optional — REJECT OPTIONALITY.** Previous digest and scoped monotonically increasing sequence MUST be mandatory; genesis is a separately authenticated explicit case.
4. **Verifier anti-rollback memory — ACCEPT.** Verification of current state requires an independently trusted latest head or protected monotonic record. Historical offline proof alone is not freshness evidence.
5. **Valid signing key compromised — ACCEPT AS TRUST-BOUNDARY LIMIT.** A compromised publisher can sign a false root. Independent witness may detect equivocation, but cannot establish truth/completeness of unobserved events. Compromised witness key/collusion is outside the claimed guarantee and must be tested as limitation.
6. **Backdating — ACCEPT.** Check timestamp ordering and bounded skew as supporting signal; never use timestamps alone as monotonic authority. Reject stale/future-dated signatures and mismatched witness sequencing according to an explicit clock/freshness policy.

## Important additional architecture constraint
Checkpoint publication is not atomic with local SQLite commit or quarantine sidecar update. Define durable `PENDING/ANCHORED/CONFLICT/UNKNOWN` states, idempotent witness CAS, crash recovery, and fail-closed admission when a quarantine transition cannot be authenticated against witness head. A checkpoint published after an unauthorized admission does not retroactively authorize it.

## Reuse investigation
The reviewer identified EP-02 modules `external_anchor`, `external_monotone_cas`, `checkpoint_store`; these are **unreviewed candidates**. Inspect contracts, credentials, monotonicity, persistence and threat assumptions before reuse. Do not assert suitability.

## Verification gates
- Fresh external witness head obtained without trusting local SQLite or sidecars.
- Q6/Q1 replay demonstrated with actual independently collected artifacts; verify HOLD before new admission.
- Two different roots at same sequence rejected; previous digest mandatory.
- Backdating, stale witness, cross-realm, revoked and compromised signer vectors covered.
- Real JUnit, raw logs, SHA-256 manifest, SUT SHA, independent reviewer.
- Provider receipt authenticity remains a separate assurance property.

**PRODUCTION HOLD. No code or tests executed by this document.**
