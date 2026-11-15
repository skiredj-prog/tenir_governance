# G4-A — Atomic simulated settlement: acceptance contract (DRAFT)

Base: G3.1 PR #72, commit `4c422b559b4d7e7b639eff275919b9d38484d1df`.
Status: experimental design and test contract only; no provider calls, production authority, cutover or REG/REG+ promotion.

## Invariants

- G4A-01: admission, current operation state, epoch, budget reservation and immutable event/outbox must be consistent in one SQLite transaction; any error rolls all back.
- G4A-02: every write revalidates the active authority epoch and durable quarantine under the cooperating JSON lock; unknown, stale, changed or unavailable means HOLD.
- G4A-03: a terminal simulated settlement is idempotent under concurrent calls, replay and restart; budget is never committed or released twice.
- G4A-04: no state transition can grant provider dispatch or external execution. Every public receipt has `execution_authorized=false`.
- G4A-05: frozen G3.1 admission snapshot remains immutable; mutable current state and outbox are versioned separately.
- G4A-06: mismatched operation, reservation, attempt, payload, epoch or trust generation must fail closed. A signed receipt is not proof of provider effect.

## Acceptance tests to implement first (RED before GREEN)

| Test | Setup / fault | Expected |
| --- | --- | --- |
| G4A-T01 | Active epoch, valid lab reservation | One current-state row and one durable event in same transaction |
| G4A-T02 | Quarantine installed between preflight and write | HOLD; zero writes |
| G4A-T03 | Epoch/source marker changed | HOLD; zero writes |
| G4A-T04 | Crash before commit | No partial state, budget or outbox |
| G4A-T05 | Crash after commit before response | Replay returns same outcome, no second settlement |
| G4A-T06 | Two processes race to settle | Exactly one logical transition and one budget settlement |
| G4A-T07 | Contradictory evidence or changed trust generation | HOLD; no terminal settlement |
| G4A-T08 | Direct SQLite budget update/commit/release bypass | Rejected unless performed through qualified epoch-bound G4-A transaction |
| G4A-T09 | Attempt to dispatch provider or set execution_authorized=true | Rejected |
| G4A-T10 | Restart with inconsistent state/event/counter | HOLD, explicit diagnostic; no auto-repair granting execution |
| G4A-T11 | Existing G3.1 admission and Raft regression suites | Remain green on exact integration SHA |

## Required sequence

1. Establish test harness and fail-closed negative controls.
2. Introduce versioned lab schema, current state and immutable outbox.
3. Implement common epoch/quarantine guard inside every write transaction.
4. Implement simulated settlement and deterministic recovery; never call provider.
5. Run scoped, G3.1, Raft and full packaging CI on exact head; independent review before any PR promotion.

## Exclusions / unresolved evidence

Inventory of deployed writers, old binaries, SDK connectors, privileged SQL/DDL, Windows and multi-host is UNVERIFIED. Existing G3.1 experimental triggers must not be silently disabled; changes to triggers require explicit negative tests. G4-F04 trust-envelope persistence and G4-F05 JSON/SQLite integration are not qualified by this tranche. No production release or merge is authorized.
