# G1 × E8/CB-05 × G4-A — Joint integration conformance matrix v0.1
**Status:** normative test design only; NOT EXECUTED; P0 OPEN; PRODUCTION HOLD.
**Scope:** POSIX single-host G1 storage daemon + G4-A SQLite settlement + CB-05 independent remote witness implementing REG E8. Multi-host Raft and real provider effects are OUT OF SCOPE.
**Provenance:** design based on G1-IPC, CB-05 and REG-ARCH-022; does not claim implementation or qualification.

## Claim and test discipline
- Record exact SUT and witness SHAs, test harness SHA, deployment topology, UIDs, credentials, OS, SQLite version, independent witness host identity, full transcripts, fault-injection barriers, negative controls, manifest hashes.
- PASS requires observed invariant at a defined linearization point; a skipped test, same-UID simulation, mocked witness, or same-host witness cannot qualify independent security.
- Independently verify the witness public key and minimum accepted head from a trust root not writable by the local operator.
- For each case record: local SQLite state, authoritative quarantine, G1 grant/epoch state, witness head, caller outcome, provider invocation count (if simulated).
- A successful local SQLite COMMIT, witness confirmation, and external effect are three different claims.

## Mandatory integration invariants
**J1 — exclusive mutation:** worker and non-engine UIDs cannot modify DB, sidecars, directory or authoritative metadata; only authenticated G1 daemon performs writes.
**J2 — authenticated admission:** signed one-time grant, current epoch, non-quarantined state and witness CAS must be bound to the *same* transaction/intent before effect eligibility.
**J3 — monotone external head:** every security-relevant transition changes the externally anchored head; rollback, equal-sequence substitution, key replacement and quarantine deletion cannot authorize a new action.
**J4 — durable idempotence:** lost responses and retries never create a second local settlement for the same operation; changed payload/realm/epoch does not inherit an unrelated grant.
**J5 — uncertainty semantics:** local commit with unconfirmed witness is PENDING (not REFUSED or operational success); unknown local outcome remains UNKNOWN until reconciled.
**J6 — fail closed:** unavailable/stale/divergent witness or compromised grant/quarantine yields no new admission.
**J7 — separation of effect:** witness and local receipt never imply an external provider effect without provider evidence.
**J8 — recovery:** any recovery/rotation/rebootstrap requires authenticated out-of-band authorization; no silent generation decrease or store reset.

## Matrix (negative tests first)
| ID | Boundary | Injection | Required observation | Invariants | Prerequisite |
|---|---|---|---|---|---|
| X01 | Worker→G1 | worker UID direct SQLite UPDATE | OS denies; no mutation | J1 | separate UIDs |
| X02 | Worker→G1 | replace DB, WAL, SHM, journal, parent directory | OS denies all writable paths | J1 | separate UIDs |
| X03 | Worker→G1 | forge SO_PEERCRED UID or signed envelope | reject before mutation | J1,J2 | real socket and keys |
| X04 | Worker→G1 | replay grant after consumption, revocation or epoch change | reject, one settlement maximum | J2,J4 | real grants |
| X05 | G1→E8 | restore valid prior state with lower sequence | HOLD_GLOBAL; no admission | J3,J6 | independent witness |
| X06 | G1→E8 | replace valid state at SAME sequence, different digest | HOLD_GLOBAL; no admission | J3,J6 | independent witness |
| X07 | G1→E8 | local state has higher sequence but unanchored chain | HOLD_GLOBAL; no admission | J3,J6 | independent witness |
| X08 | G1→E8 | substitute entire trust root and receipt key | independent verifier rejects | J3,J8 | external pins |
| X09 | G1→E8 | delete quarantine JSON/sentinel or restore old quarantine | no admission; anchor mismatch | J3,J6 | quarantine anchored |
| X10 | G1→E8 | race quarantine/epoch revocation with grant consumption | one authoritative linearization; revoked-first cannot execute | J2,J3 | CAS and durable grant |
| X11 | G1→E8 | witness unreachable at boot or during admission | fail-closed; no new admission | J6 | independent witness |
| X12 | G1→E8 | two engines concurrently CAS same head | at most one winning transition; loser HOLD/reconcile | J3,J4 | linearizable CAS |
| X13 | G4-A→E8 | crash before local SQLite COMMIT | no committed local settlement; retry safe | J4,J5 | deterministic barrier |
| X14 | G4-A→E8 | crash after local COMMIT, before witness advance | durable local settlement; PENDING, HOLD; recovery no double settlement | J4,J5,J6 | deterministic barrier |
| X15 | G4-A→E8 | witness advance persisted, response lost | read head; reconcile; no second advance or effect | J3,J4,J5 | fault-injected ACK |
| X16 | G4-A→G1 | two concurrent requests same business key, different request IDs | at most one local settlement | J2,J4 | multi-process harness |
| X17 | G4-A→G1 | post-COMMIT local receipt write failure | never report pre-effect refusal; status resolves correctly | J4,J5 | receipt fault |
| X18 | E8→provider | confirmed witness head, provider times out | provider outcome UNKNOWN, not CONFIRMED | J7 | provider simulator |
| X19 | Recovery | witness corrupted/retired; attacker proposes lower head | reject; require signed ceremony and new store_id where applicable | J8 | independent operator |
| X20 | All three | restore DB+quarantine+trust together while daemon live | next critical transition denied; no stale grant accepted | J2,J3,J6 | active adversarial runner |
| INT-E8-01 | G1↔E8↔G4-A | stale epoch or divergent head submitted to synchronous pre-authorization | authenticated HARD_HOLD before business write/dispatch; see sub-vectors 01a–01h | J2,J3,J4,J5,J6 | independent witness, real grant, atomic CMT reservation |

## INT-E8-01 — Synchronous pre-authorization rejects a restored or divergent authority (RED-first)

**Boundary:** G1 privileged storage engine ↔ independent E8/CB-05 witness ↔ G4-A durable settlement.

**Claim to falsify:** no irreversible provider dispatch and no new business settlement/idempotency consumption is permitted under a stale epoch, a rolled-back local head, or a divergent head **before** an authenticated, operation-bound authorization is durably established.

**Preconditions:** a known-good witness head exists under externally pinned witness keys, independently retained on a second host; engine and worker use distinct OS identities; valid signed worker envelope and grant are supplied for the *unmodified* case; instrumented business-write boundary, external-dispatch counter and event recorder are observable. The test harness controls the adversarial local snapshot but **not** witness state, keys or the trusted-head pin.

**Negative stimulus:** replace a locally valid snapshot by a prior epoch (example 5 → 4) **or** keep the epoch/sequence while changing `state_digest`/`quarantine_digest`; submit a signed `COMMIT` for that stale authority. A `PRE_AUTH_CHECK` that only compares timestamps or epoch numbers is insufficient.

**Required handshake/oracle:**
1. G1 validates `SO_PEERCRED`, envelope and grant, binds realm/store, trust generation, epoch, authoritative quarantine head, request ID, transaction ID, payload digest, grant digest and proposed state transition to a unique authorization ID; no business write has occurred.
2. G1 submits an authenticated, bounded-time `PRE_AUTH_CHECK` to E8 with the **expected previous witness-head digest** and proposed transition. E8 evaluates an atomic compare-and-advance (or equivalent durable reservation) at the relevant linearization point. A read-only `get_head` followed by an unprotected local commit **does not pass**.
3. If E8 rejects a stale epoch, rolled-back or equal-sequence divergent head, G1 records `E8_ROLLBACK_DETECTED` or `E8_HEAD_DIVERGENCE` and enters `HARD_HOLD`; no business/idempotency rows are created or changed and provider dispatch count remains zero. An independently protected security/HOLD event is allowed and desirable; therefore *zero file/SQLite writes* is **not** the acceptance criterion.
4. If witness times out or acknowledgment is lost, G1 returns `WITNESS_OUTCOME_UNKNOWN` / `HOLD_GLOBAL`, never `REFUSED_BEFORE_EFFECT` or `CONFIRMED`; it reconciles the witness authorization ID before retry. A rejected signed response must itself be authenticated.
5. If approved, G1 verifies the CMT's witness signature against **externally pinned** keys and scope (realm/store, audience, trust generation, epoch, previous head, state/quarantine/grant digests, operation/request/transaction IDs, authorization ID, expiry) and durably consumes its one-time right before allowing dispatch. A signed but expired, previously used, swapped or superseded CMT is rejected. The CMT cannot be a transferable bearer permit.
6. **Concurrency/revocation:** race a second actor's epoch revocation or quarantine transition against the CMT; success is permitted only if the action's authorization and the revocation have a well-defined common ordering. No effect on a grant already invalidated at the authorization/dispatch boundary.

**Sub-vectors and expected results:**

| Case | Injection | Oracle |
| --- | --- | --- |
| INT-E8-01a | Epoch 4 presented while external head is at 5 | Authenticated `E8_ROLLBACK_DETECTED`; HARD_HOLD; zero business commits and dispatches |
| INT-E8-01b | Same epoch/sequence, different state digest | `E8_HEAD_DIVERGENCE`; HARD_HOLD; zero business commits and dispatches |
| INT-E8-01c | Witness unavailable, maliciously slow or ACK lost | UNKNOWN/HOLD, zero dispatch; query authorization status before retry |
| INT-E8-01d | E8 approves and returns fresh one-time, fully bound CMT | At most one eligible local settlement; check external head and CMT binding; **does not** establish external provider effect |
| INT-E8-01e | Replay, wrong realm/transaction/payload, expired token or forged witness key | REJECT/HOLD; no additional settlement or dispatch |
| INT-E8-01f | Revocation/quarantine races with CMT issuance and dispatch | Invalidated-first action denied; verified unique linearization and no post-revocation dispatch |
| INT-E8-01g | Crash after witness reservation, before SQLite COMMIT; restart/replay | No untracked effect, recover/release or reconcile reservation without double settlement |
| INT-E8-01h | Crash after SQLite COMMIT before witness settlement confirmation | LOCAL_COMMITTED_WITNESS_PENDING, no dispatch until reconciled, never a false pre-commit refusal |

**Required evidence:** exact witness and G1/G4-A SUT SHAs, signed witness transcript, externally verified head and key pin, full pre/post SQLite business-table snapshot and idempotency count, a durable security/HOLD event, dispatch call count, precise fault-injection barriers, complete request/CMT digests, independent host/UID topology, negative-control result and raw test logs.

**Verdict:** PASS only if *all* normative sub-vectors pass against the real G1↔independent E8↔G4-A system at the stated trust boundary; FAIL if any rejected case makes a business commit or dispatch, or a stale/replayed CMT authorizes action. `MOCK_PASS` is exploratory and **never** satisfies this integration gate; SKIP/UNKNOWN → **PRODUCTION HOLD**.

**Protocol gap / gating decision:** the proposed `PRE_AUTH_CHECK` and signed CMT are **not implemented** in G1. A read-only check followed by a local commit has a TOCTOU window; witness reservation before local commit introduces crash/compensation ambiguity. Do not declare INT-E8-01 resolved until a single-use, operation-scoped, externally attested reservation/consumption protocol and its failure windows are tested. Witness pinning remains independent of the local storage operator.

## Experimental implementation trace (PR #79; NOT conformance evidence)

An isolated implementation now exists on the **separate draft PR #79**:
- `experimental/g1_ipc_prototype/e8_lab.py`: SQLite-monotone witness, actual loopback HTTP, Ed25519 signed requests/attestations and pinned witness verification in a test fixture.
- `experimental/g1_ipc_prototype/g1_e8_guarded.py`: separate opt-in G1 Unix-socket prototype, witness pre-authorization prior to local G1 commit.
- `experimental/g1_ipc_prototype/test_g1_e8_live.py`: exploratory INT-E8-01a/b/c/d/e/g scenarios and live Unix socket check; XFAIL deliberately demonstrates that privileged modification of business tables **without changing the cached head is not detected**.
- `experimental/g1_ipc_prototype/README.md`: exhaustive limitations and test commands.

**Updated exploratory evidence (2026-10-26):** GitHub CI [run 40269372473](https://github.com/skiredj-prog/tenir_governance/actions/runs/40269372473), G1 PR #79 commit `3fcae6b7da7d162e776b0dd29055b237d20cf1da`: **24 PASS, 2 XFAIL**, no unexpected failures. The third RED scenario (privileged business-table tamper without cached-head change) is now PASS with real SQLite snapshot reconstruction and witness-bound predicted post-state digest. The 2 remaining XFAIL cover the original unguarded G1 path (no witness preauthorization). This is same-host loopback-only exploratory evidence; **not** independent security conformance.

**Critical deviation from normative INT-E8-01:** the added canonical integrity guard covers the G1 *demo* business and idempotency tables, not the full G4-A business/quarantine state or same-UID mutation of executable engine code. It has neither a second independently controlled host nor TLS/mTLS, nor an actual signed one-time grant, nor provider dispatch. A real independent witness cannot by itself prove the engine truthfully measured its own state. It does not satisfy the cross-boundary PASS oracle; R-01/Q1/Q6/C1/C2 remain P0 open.

## Transaction boundary design decision (blocking)
A post-local-commit witness CAS cannot retroactively prevent a provider effect already dispatched. The implementation MUST establish a **pre-effect attested authorization** or an equivalent proven protocol. Local SQLite and remote witness cannot be claimed as one atomic transaction. Define ordering and recovery for: grant reserve → witness authorization anchor → local commit → witness settlement anchor → effect dispatch → provider receipt. This is a *candidate sequence*, not an approved protocol; failure windows at every arrow must be formally resolved before dispatch.

## Execution phases and exit criteria
- **Phase A, red-first:** X01–X04 on real separate UIDs; no witness required. Evidence does not qualify E8.
- **Phase B, remote witness:** X05–X12 and X19; use separate host/identity/persistence and independent pin provisioning.
- **Phase C, commit integration:** X13–X17 with deterministic crash barriers, exact durable receipts and no re-execution.
- **Phase D, end-to-end:** X18/X20 plus concurrent replay and quarantine; all J1–J8 independently adjudicated.
- **Go/no-go:** all mandatory tests PASS on fixed SUT SHA, independent witness verified, no skipped security tests, adversarial reviewer signs off, production operations/recovery documented. Any FAIL/UNKNOWN/SKIP on a mandatory security test means **PRODUCTION HOLD**.

## Explicit open issues
No external witness implemented or integrated yet; G1 OS isolation harness is not executed; grants/revocation not implemented in prototype; G4-A remains experimental; no external effect guarantee; Raft FM-20 remains separately open. This matrix is a test contract, not an evidence record.
