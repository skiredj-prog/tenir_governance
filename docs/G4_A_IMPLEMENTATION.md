# G4-A — Atomic simulated settlement, laboratory implementation

Status: **experimental POSIX single-host; no provider dispatch or production authority**.
Acceptance baseline: `eccaf91b0450fb8ecaaefa009427022233a7d9af`, based on G3.1
`4c422b559b4d7e7b639eff275919b9d38484d1df`. See [the original contract](G4_A_ACCEPTANCE_DRAFT.md).
This document specifies scope and mechanisms; executed results belong to the
validation report for an exact candidate SHA, not to this prose.

## API and transaction boundary

`ExperimentalG4AAuthority.provision(backend, trust_generation=1,
experimental_only=True)` requires an activated, empty G3 laboratory database.
It refuses existing reservations, foreign schemas and repeat provisioning.
`open(...)` checks the schema/epoch/trust and complete state; it does not migrate
or reconstruct missing G4 data. The supplied legacy `SQLiteConsensusBackend`
constructor still performs its own existing initialization; failure of G4
validation never creates G4 admission or grants from that initialization.

The original G3 source and admission snapshot stay immutable. New tables:

- `g4a_meta`: immutable schema fingerprint, epoch and trust generation.
- `g4a_current`: versioned simulated current state, reservation and attempt binding.
- `g4a_commands`: immutable request digest, canonical inputs and original receipt.
- `g4a_outbox`: immutable hash-linked events, one per committed command/version.

`admit_created(record, attempt_id=..., request_id=...)` writes intent,
reservation, budget counters, snapshot, current state, command result and event
in **one SQLite transaction**. `settle(request_id, [SimulatedEvidence(...)])`
changes current state, reservation, counters and event in one transaction.
There is no intermediate grant and no provider callback. `inspect(key)` and
`inspect_all()` also validate the full store before returning receipts.

Lock order: durable quarantine gate → cooperating JSON writer lock → SQLite
`BEGIN IMMEDIATE`. The G3 frozen source, markers and epoch are checked inside
the transaction. Durable quarantine is checked inside the transaction and again
before commit, while its shared gate is held through commit. Activation in
another process therefore linearizes before or after a settlement. No network
I/O runs under these locks. SQLite busy waits and whole-store validation still
affect latency; no emergency-lock latency SLA or load qualification is claimed.

The existing quarantine sidecar protocol remains the shared authority in this
tranche; a native SQLite quarantine migration is **not** implemented. This is
atomic decision coordination by a common gate, not a claim that JSON sidecars
and SQLite share one filesystem transaction. Unknown/corrupt/release-pending
quarantine blocks admissions, settlements, replays and normal inspection.

## Simulated outcomes

| Input | Current operation | Budget | Meaning |
|---|---|---|---|
| Admission | RESERVED | RESERVED | Lab capacity retained; no execution permission |
| UNKNOWN | SIMULATED_UNKNOWN | RESERVED | Synthetic uncertainty; no timeout release |
| EFFECT_CONFIRMED | SIMULATED_CONFIRMED | COMMITTED | Simulated accounting only |
| NON_EXECUTION_CONFIRMED | SIMULATED_FAILED | RELEASED | Simulated pre-dispatch accounting only |

These are **not** the live G4-B DISPATCHING/UNKNOWN semantics. No effect is sent,
so this tranche neither refunds a real dispatched operation nor settles a real
provider. It must not be adapted to live execution by relabeling input outcomes.

All receipts have immutable constructor defaults: `decision=HOLD`,
`execution_authorized=false`, `budget_settlement_authorized=false`,
`is_production_authority=false`, `simulated=true`. A budget row becoming
COMMITTED is not a real payment confirmation or an execution grant.

Identities include operation, reservation, attempt, payload digest, active epoch
and trust generation. The source snapshot binds the original input. A repeated
request ID with identical canonical input returns its original receipt, even
after later transitions; a changed input under that ID is rejected. A terminal
replay with identical evidence under another request ID returns the terminal
receipt without another event or settlement. Changed terminal evidence is HOLD.
Contradictory batches are rejected without writes; this tranche does not create
a production incident/compensation workflow or implement T8.

`SimulatedEvidence` is deliberately a synthetic typed input. It is not signed
provider evidence. The complete synthetic input is persisted for replay, but
issuer verification, key lifecycle, proof expiry, LEI/nonce/provider finality,
policy re-adjudication and real trust-envelope persistence remain unqualified
(G4-F04). The trust generation is immutable per lab store; rotation or mismatch
requires HOLD, not automatic rebinding. Epoch rotation/live cutover is absent.

## SQL guards and their limit

Provisioning explicitly replaces `experimental_g3_no_budget_transition` in the
same transaction as the G4 schema installation. Other G3 immutable guards remain.
New write triggers on authority tables require a connection-local SQLite
function installed only after the G4 transaction checks. Ordinary connections,
legacy budget APIs and the old native admission API cannot perform these writes.
Outbox, command and metadata updates/deletes remain forbidden even through the
normal guarded connection. Schema fingerprinting detects missing/changed guards.

This is **not a sandbox against hostile Python, privileged SQL/DDL or the owner
of the database file**. A privileged client can register the same function,
remove triggers or rewrite the store. Negative corruption tests explicitly use
such privileges to check fail-closed restart; they do not demonstrate that the
tampering itself is impossible. Deployed-writer inventory/OS permissions remain
a production gate. Normal G3.1 stores and their default behavior are unchanged.

## Acceptance mapping

| Contract | Test coverage source |
|---|---|
| T01 | Atomic admission, unchanged source/snapshot, stable replay |
| T02 | Quarantine between preflight and write, both interprocess orderings, corrupt/pending quarantine |
| T03 | Marker/source/sentinel/epoch drift, including replay after terminal commit |
| T04/T05 | Abrupt process exit after budget write, before commit, after commit-before-response; admission and both settlements; schema-provision crash |
| T06 | Two separately spawned settlement processes, one logical terminal event/accounting mutation |
| T07 | Conflicting evidence, wrong immutable trust generation, terminal contradiction, changed identities |
| T08 | Direct SQLite APIs/SQL, old admission path, post-settlement refund, new reserve, protected resources/events/commands |
| T09 | No provider/dispatch surface, provider calls trapped, receipt escalation rejected |
| T10 | Counter/event/snapshot/schema corruption refuses restart; no auto-repair |
| T11 | Existing G3.1, Raft and complete tests remain separate executed suites; not implied by targeted results |

Test file: `tests/test_g4a_atomic_simulated_settlement.py`. Abrupt `os._exit`
tests are process-crash tests, not physical power-loss tests. Existing assertions
are preserved. Full Windows qualification, multi-host, load, real provider,
REG/REG+ and publication are outside this tranche. FM-20 remains open.
