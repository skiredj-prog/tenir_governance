# G1-IPC — independent OS boundary and crash-recovery test plan v0.1
**Draft PR #79 · non-production test contract · 2026-10-25**

## Isolation from concurrent WIP
No modifications to G4-A or EP-02 source, no merge, no production integration. Tests target only `experimental/g1_ipc_prototype/` and a disposable Linux sandbox. Keep the original SUT SHA and G1 test environment SHA separately recorded.

## Critical gap inventory
1. `SO_PEERCRED` authenticates a connecting Unix peer, **not** a cryptographic authorization grant. The current `grant_signature=NOT_IMPLEMENTED` is intentionally inert; do not treat the daemon as an enforcement boundary for business authority.
2. Socket path permissions are not sufficient to prove that the worker UID cannot directly write or rename the SQLite database, WAL, SHM, journal or directory.
3. A single SQLite local commit does not prove an external provider effect occurs at most once.
4. Local `request_id` replay protection must not be confused with business-operation uniqueness across epochs/principals; the current local commit key `(realm, transaction_id)` intentionally spans epochs but needs formal semantics.
5. The current prototype implements `COMMIT` only, no `STATUS` and no persisted authorized `ADMIT` prerequisite; therefore it cannot be used as the production G1-IPC v0.2 contract.
6. The current server processes requests serially and does not demonstrate races with multiple server processes or crash/restart recovery.

## Sandbox topology
- UID `g1_engine`: sole owner of DB directory, DB, WAL, SHM and socket server process.
- UID `g1_worker`: can connect to an engine-owned Unix socket but cannot create, modify, unlink or replace DB/sidecars or parent directory.
- UID `g1_controller`: can start tests and collect evidence but is not a runtime worker.
- Use dedicated disposable directories; never run the adversarial suite as root or under the engine UID and claim worker isolation.
- Record `id`, `stat`, ACLs, mount options, filesystem, umask and relevant MAC policy; test actual syscalls and their errno.
- An engine-UID compromise remains outside this boundary; CB-05 is separately required for rollback-resilient witness claims.

## Test matrix
| ID | Actor | Stimulus | Expected |
| --- | --- | --- | --- |
| OS-01 | worker UID | SQLite INSERT/UPDATE directly | OS denied; no DB change |
| OS-02 | worker UID | `os.replace` DB, WAL, SHM and journal, including absent-sidecar cases | OS denied |
| OS-03 | worker UID | Create temp file and rename over DB in parent directory | OS denied |
| IPC-01 | authorized worker UID | signed, canonical `COMMIT` via socket | one durable local row |
| IPC-02 | unauthorized UID | connect and send valid-looking request | denied |
| IPC-03 | worker UID | forged `auth.worker_uid` or tampered signed payload | denied |
| IPC-04 | worker UID | duplicate-key JSON, oversized frame, invalid UTF-8, unknown operation | denied, no mutation |
| IDEM-01 | worker UID | identical replay after response loss | same receipt, one commit |
| IDEM-02 | worker UID | same request ID with changed payload | CONFLICT, no additional commit |
| IDEM-03 | worker UID | new request ID, same transaction, different epoch | CONFLICT, one commit |
| CRASH-01 | engine | SIGKILL before transaction commit | zero commits; retry may commit once |
| CRASH-02 | engine | SIGKILL after durable commit, before response | recovery returns existing result, one commit |
| CRASH-03 | engine | SIGKILL after response | durable result survives restart |
| GRANT-01 | worker UID | unsigned/expired/revoked grant | HOLD/DENIED; **pending implementation** |
| QUAR-01 | worker UID | quarantine races with commit | HOLD if quarantine linearizes first; **pending implementation** |

## Crash harness requirements
Inject deterministic pause points **before SQLite COMMIT**, **after COMMIT and before socket response**, and **after socket response**; kill the engine process with SIGKILL at each. Reopen database in a fresh engine process, inspect ledger and commit rows, and verify retry behavior. Do not use arbitrary sleeps as evidence of a particular crash point; require a barrier/ack to the test controller.

For the current prototype, a lost socket response can be resolved only by replaying the same signed request; `STATUS` is not implemented. The official contract must add independently authorized STATUS with immutable receipt retrieval, and distinguish UNKNOWN from known rollback.

## Gate and evidence
- Pre-gate: inspect sandbox UID/ownership topology and run OS-01/02/03 negative tests.
- Prototype mechanics: IPC-01..04, IDEM-01..03.
- Crash gate: CRASH-01..03, deterministic barrier evidence and restarted daemon.
- Security gate: GRANT-01 and QUAR-01 require real grants and authoritative quarantine; cannot pass via a placeholder.
- Preserve full command lines, exact commit SHA, Python/SQLite/kernel/filesystem versions, logs, JUnit, exit codes, negative controls and SHA256 manifest. Report partial passes as partial only.

**Status: TEST PLAN COMMITTED / CROSS-UID AND CRASH TESTS NOT RUN / G1 ISOLATION UNPROVEN / PRODUCTION HOLD.**
