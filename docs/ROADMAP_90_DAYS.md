# TENIR-Gov — 90-day release roadmap

**Planning window:** 19 October 2026–17 January 2027 (indicative; Harv review and capacity may change sequencing).
**Release rule:** no merge, tag, or publication until its stated evidence and approvals are complete. Review means independent review, not certification.

## Release sequence

| Window | Milestone | Scope and exit evidence | Explicit boundary |
|---|---|---|---|
| Days 0–30 · 19 Oct–18 Nov | **5.7.0 — Bounded Mono-Host Core** | Close P0 I8, deterministic Raft inputs, finite positive amount validation, SQLite WAL atomicity and recovery; demonstrate I2 payload binding and I5 conflict-to-HOLD; execute the full suite including Raft; validate the deployment manifest; complete branch review and Harv's independent review. | Targets single-host deployments with explicitly bounded guarantees. FM-20 stays **Open**, `production_allowed: false`, target 5.8.0. Multi-host startup and production Raft are hard-disabled. No certification claim. |
| Days 31–60 · 19 Nov–18 Dec | **5.7.5 — Auditable Evidence & Pilot Readiness** | Produce a reproducible, versioned evidence package tied to an exact source commit, package build, deployment profile, manifest, invariant-map version, and test/dependency inventory. Record actual executions, decisions and state transitions with context and timestamps; link EvidenceRecords/receipts to source events; retain provenance and SHA-256 hashes. Export a coherent HTML report, PDF, and JSON-LD evidence graph from the same report data. Re-run from a clean environment; rehearse manifest validation, SQLite backup/restore, incident escalation, and operator runbook. Assess a bounded mono-host pilot with customer acceptance and explicit operating limits. | The report distinguishes file integrity, structural conformance, observed behavior, and guarantees demonstrated within a stated scope. Hashes establish file integrity only; they do not prove correctness, safety, completeness, or regulator approval. A paid mono-host pilot may proceed once its evidence and operational prerequisites are accepted; multi-host readiness is not a prerequisite. FM-20 remains Open. |
| Days 61–90 · 19 Dec–17 Jan | **5.8.0 — Multi-Host Hardening** | Replace or isolate PySyncObj's pickle command transport with a bounded, schema-validated protocol; define authenticated peer identity and key rotation; test malformed/replayed messages, partition/rejoin, crash recovery, rolling upgrades, and adversarial peer behavior; run multi-host fault campaigns and an independent security review. Update the manifest and threat model only against completed evidence. | Multi-host production remains disabled until each gate is demonstrated and approved. A target date or passing unit suite alone does not close FM-20. |

## Sequence gates

1. **5.7.0:** single-host accounting and recovery evidence is reproducible; production startup requires a verified manifest; the multi-host kill-switch is tested at manifest, CLI, API, and Raft-constructor boundaries.
2. **5.7.5:** one versioned evidence source produces consistent HTML, PDF, and JSON-LD; provenance, report hashes, scope, and limitations are checked from a clean run; the pilot deployment profile, backup/restore, ownership, incident response, and rollback procedures are accepted by the pilot owner.
3. **5.8.0:** FM-20 changes from Open only after the replacement transport, peer authentication, and multi-host campaign are independently reviewed. Production enablement is a separate explicit release decision.

## Harv review timing

Respect the instruction **“no dossier before 5.7”**: do not assemble or send an external Harv validation pack before v5.7.0 is published. The pre-release independent review uses the candidate branch, exact commit SHA, and CI evidence directly. After publication, any external pack must identify that exact released source/package and separate observed evidence from claims. This timing does not authorize a merge, tag, publication, or external send by itself.

## Evidence report language

Use: “Observed on commit/package X under test environment Y; report SHA-256 Z; scope and limitations listed below.” Avoid “certified,” “proven safe,” and any claim that a hash establishes the correctness of the implementation or approval by a regulator.

## Current execution state

- Candidate branch: `codex/v5.7.0-rc`, based on durable-budget base `32c3b7a`; PR #18's HOLD code delta is included once. PR #19's test-only commit and reproduction file are excluded from candidate history and tree.
- This roadmap is a planning artifact, not a test ledger. Exact-SHA commands, results, skips, and limitations belong in the separate release-readiness report.
- A pre-consolidation Git stash preserves the initial staged, unstaged, and untracked changes. The ignored deployment schema was preserved in place and is being added explicitly to the candidate.
- Open gates: exact-SHA remote CI; completion of the broader SQLite crash-recovery campaign; independent review of the full diff; and Harv's independent review.
- FM-20 remains **Open** until 5.8.0 evidence is complete. Production multi-host and production Raft remain blocked; the current mitigation is a hard block, not a transport fix.
- No external Harv dossier is to be created before v5.7.0 publication. Do not merge, tag, publish, or create a Zenodo release as part of this consolidation.
