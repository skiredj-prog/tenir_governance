# CB-05 / EP-02 audit handoff and evidence intake
Date: 2026-10-25 | **REPORTED AUDIT, NOT INDEPENDENTLY VERIFIED** | PRODUCTION HOLD

## Source baseline and provenance
- SUT baseline: `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`.
- External operator reports 15 EP-02 test files, 190 pytest passes, zero failures, clean clone; evidence held outside repository in `/home/claude/r02/ep02/`. **This document has not inspected those files or rerun the tests.**
- Operator reports R-02 Q6/Q1/P6 reproduction in `/home/claude/r02/` by same operator, not independent reviewer.
- Existing CB-05 documents on this branch: `CB05_INDEPENDENT_CHECKPOINT_ARCHITECTURE_v0.2.md`, `CB05_CHECKPOINT_SPEC.md`, `CB05_ADVERSARIAL_REVIEW_DISPOSITION_2026-10-25.md`, `CB05_WITNESS_PROTOCOL_DECISION_2026-10-25.md`. Earlier baseline contracts live on `draft/g4a-r01-cb05-negative-contracts`. Verify versions against actual blob SHAs before publishing a canonical compilation.

## EP-02 reuse triage (operator-reported code/test review)
| Component | Triage | Limitation / next verification |
| --- | --- | --- |
| Ed25519 request, receipt, anchor and trust-snapshot verification | Reuse candidate | Inspect trust root, key provenance, revoked key paths and binding |
| Request IDs, challenges, replay flag | Reuse candidate | Validate cross-realm/epoch and concurrent replay |
| Unknown-outcome lookup after lost response | Reuse/extend | Real network faults and witness-host crash not tested |
| 17 fault injection points | Reuse candidate | Inventory actual seams and recovery oracles |
| Signed trust snapshots / rollback floor | Adapt | Local floor does not resist coherent rollback |
| SQLite durable CAS | Adapt | Local privileged writer can bypass file/DDL protections |
| `assess_anchor` | Adapt | Caller-supplied observation, no independent freshness or signature verification |
| `ep02_checkpoint_store` | Replace as witness | Local JSON high-water only; can be used as restart cache, not trust root |
| Key provisioning/rotation/protection | Design new | Private keys injected by caller; test-generated keys are not operational custody |
| `rollback_witness` test | Adapt | Same host/process/root key is not independent trust-domain proof |

## Mandatory second-pass review
1. Read EP-02 tests and code for Ed25519 key custody, identity pinning, rotation/revocation, signature domain separation, canonicalization, replay and rollback floor.
2. Run exact-SHA EP-02 tests independently with raw logs, JUnit, exit status, runtime/environment and SHA-256 of actual files; keep pytest PASS separate from security verdict.
3. Audit witness persistence, CAS monotonicity and idempotent recovery across *two hosts/identities*, including network partition, witness restart, and conflicting heads.
4. Verify quarantine digest/epoch binding and freshness at admission linearization; test Q6/Q1 fail-closed before unauthorized admission.
5. Independently review evidence from R-02 and EP-02 before upgrading status.

## Evidence intake format (no invented artifacts)
Publish on a dedicated evidence branch, not the SUT or architectural branch, with:
```
evidence/ep02/<run-id>/
  provenance.md        # operator, SUT SHA, environment, command, exit status, scope/limits
  pytest.log
  junit.xml
  manifest.sha256      # sha256sum of actual files, generated after execution
evidence/r02/<run-id>/
  provenance.md
  harness.py
  results.json
  pytest.log
  junit.xml
  manifest.sha256
```
For sensitive logs, redact credentials before publication and record redaction hashes/provenance. Never publish private keys or real user data. A copied narrative is not raw test evidence. GitHub issue #76 remains open until a different operator reproduces the tests.

## Decision
EP-02 protocol elements are candidates for reuse, **not** proof of CB-05 G2. Independent witness implementation, key custody, freshness and adversarial test evidence remain open. No merge/deployment. **PRODUCTION HOLD**.
