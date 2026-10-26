# CB-05 witness protocol decision — 2026-10-25
**ARCHITECTURAL DECISION / NO PROTOTYPE / PRODUCTION HOLD**

## Decision
CB-05 G2 assurance requires an authenticated witness in a **separate host, OS identity and persistence/trust domain** from G4-A. A second SQLite/JSON store on the same host or under the same compromised credentials is not independent. No G2 assurance claim before deployment and adversarial verification.

## Availability and admission policy
- For G2-required admissions, unreachable, stale, conflicting, or unverifiable witness head means **HOLD**. Do not downgrade silently.
- Degraded operation is a separate, explicitly authorized policy and cannot be labelled G2-assured.
- Freshness bounds, retry deadlines, clock skew and availability SLAs remain parameters to be fixed and tested.
- A periodic checkpoint is insufficient to prove that a newly activated quarantine was enforced before an admission. Define a **linearization protocol**: a quarantine transition must obtain a durable monotonic witness acknowledgment before subsequent admissions may rely on a later head; unacknowledged/unknown transitions HOLD. Analyze races and process crashes explicitly.

## Candidate protocol (design, not implemented)
1. Producer prepares canonical checkpoint including realm/store, epoch, monotone sequence, mandatory previous digest, event Merkle root, quarantine generation/digest, and commit boundary.
2. Witness verifies authenticated submitter, scope, sequence, previous head, and CAS/idempotency; durably persists new head **before** returning signed acknowledgment.
3. Verifier obtains latest head through an independent authenticated channel, validates witness signature, scope, continuity and freshness; compares canonical local evidence and quarantine state.
4. For a lost ACK, producer queries witness by scoped request ID and digest, never assumes a failed network response means no commit.
5. Competing checkpoints at the same sequence are conflict/fork, not silent last-writer-wins.
6. Signer key compromise and witness compromise are separate threat cases; a valid signature alone does not establish truth/completeness.

## EP-02 audit status
First-pass code review **reported**, not independently reproduced by this document:
- `ep02_external_anchor`: pure revision/hash comparison; no authenticated freshness source.
- `ep02_external_cas`: Ed25519 signed request/receipt protocol, local durable CAS dependency.
- `ep02_durable_cas`: local SQLite and triggers; not privileged-writer isolation.
- `ep02_checkpoint_store`: local JSON high-water mark, not independent witness.

**No reuse decision yet.** Required next: inspect actual EP-02 tests, key provisioning, signature verification, root-of-trust and replay semantics; run tests at exact SUT SHA `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3` with real JUnit, stdout/stderr, environment and SHA-256 manifest. Report test PASS separately from security assurance.

## R-02 evidence handling
Q6/Q1/P6 second harness results are reported by the same operator; raw files remain outside repository at `/home/claude/r02/`. Independent reproduction and artifact review remain pending under #76/#78. Never present these results as independently verified.

## Gates
G1 OS isolation and explicit privileged attacker model; G2 fresh independently durable monotonic witness; actual Q1/Q6, rollback/fork and commit/ack recovery tests; independent review. No merge/deployment. **PRODUCTION HOLD**.
