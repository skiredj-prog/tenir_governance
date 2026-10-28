# PROOF-01 — Agent 1 / Codex W2 implementation contract

**Status:** experimental LAB-S1 integration only. Not certification, not deployed provider, production HOLD. Parent source lock: PMO Draft PR #83.

## Required single-owner architecture

- G4-A is the mandatory ingress for grant authorization and all provider dispatch. Direct provider credential/SQL bypass is still OPEN until structurally fenced and adversarially tested.
- B3 is the **simulated external provider** owning durable effect truth within its own fixture ledger. Its Ed25519/Merkle receipt must remain unmodified; no adapter may re-sign unsigned RC-01/I8 observations.
- REG-RC-01 owns state transitions, including UNKNOWN and REPAIR. Its v0.1 document is a candidate, not ratified.
- I8 is the only authority to reserve, commit and release budget/capacity. **Do not duplicate its mutable ledger by adding a second "G4-A budget" or another "RC-01 budget".** Adapt existing G4-A/I8 state ownership carefully.
- Witness-first protocol is OPEN B2; distinct SQLite file is not an independent custodian. Shared-host means LAB-S1, not independent IND-M1.

## Minimal scenario

Grant -> durable I8 reservation -> bound durable dispatch intent -> provider executes once -> ACK lost -> UNKNOWN with reservation intact -> provider-signed EXECUTED proof verified -> forced I8 settle failure -> REPAIR -> actual process crash (SIGKILL) -> reopen persisted stores -> idempotent I8 commit exactly once -> RECONCILED_CONFIRMED, no second dispatch. Any uncertain or negative evidence stays UNKNOWN unless provider finality is proven. Old grants never resurrect.

Instrument failed/ambiguous post-commit acknowledgement, concurrent same-ID and different-ID recovery, attempted RELEASE from REPAIR, same-UID SQL bypass, sidecar quarantine rollback and witness failure. Never infer real power-loss durability from SIGKILL.

## CEB-01 deliverable

```text
proof-manifest-v1.json
convergence.json
deployment-manifest.json
manifest.json
states/operation-states.jsonl
events/g4a-events.jsonl
events/i8-events.jsonl
receipts/<operation_id>.receipt.json
witness/<operation_id>.witness.jsonl
reports/test-results.json
reports/proof-obligations.json
open-items.json
label.txt
```

Use the full integrated 40-character SHA, deployment-manifest SHA-256 and topology ID in all manifests. Require B1/B2 OPEN. Label: "Intégration laboratoire W2 — non certifiant — contournements B1/B2 connus et ouverts". Raw source receipt and test evidence must remain present, including negative test **SECURITY_FAIL** when the attack runner passes.

## Acceptance order

1. Reproduce B3 lab original tests (27 provider + 31 integration tests = 58 PASS, 4 SKIP/OPEN on Linux baseline; not guaranteed on new SUT).
2. Compile genuine G4-A and preserve original scoped tests; no mock G4-A in place of actual code.
3. Run lost ACK, crash, restart, race and unauthorized bypass tests against exact new source SHA.
4. Export CEB-01 and verify using independent W4 reader without runtime calls. A verification error or INDETERMINATE cannot be relabeled VERIFIED.
5. Send full handoff to Agent 0/PMO [ticket #81](https://github.com/skiredj-prog/tenir_governance/issues/81), retain E2E HOLD until accepted. W3 LAB-S1 work is not structural witness independence.

No merges, RFC ratification, real banking effect or production cutover are authorized by this document.
