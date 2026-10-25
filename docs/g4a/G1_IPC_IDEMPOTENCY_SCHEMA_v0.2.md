# G1-IPC v0.2 — Durable idempotency and JSON wire contract
**DESIGN / NOT IMPLEMENTED / NOT TESTED / PRODUCTION HOLD**
Supersedes no v0.1 security boundary; refines persistence and wire behavior for a disposable POSIX sandbox.

## Security and atomicity scope
A correctly isolated engine process is the sole writer of the SQLite database. SQLite transaction durability is assumed under its documented filesystem/fsync behavior. This contract guarantees **at most one local committed transition per scoped idempotency identity**, not exactly-once remote provider effects. The local engine UID/root attacker, coherent DB rollback, compromised issuer, and unavailable CB-05 witness remain separate threats.

## SQLite schema (prototype)
```sql
PRAGMA foreign_keys=ON;
CREATE TABLE ipc_requests (
  realm_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  epoch INTEGER NOT NULL CHECK(epoch >= 0),
  principal_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('ADMIT','COMMIT','CANCEL')),
  idempotency_key TEXT NOT NULL,
  request_id TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','ADMITTED','COMMITTED','CANCELLED','HOLD','DENIED')),
  response_json TEXT,
  receipt_digest TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (realm_id,store_id,epoch,principal_id,operation,idempotency_key),
  UNIQUE (realm_id,store_id,epoch,request_id)
);
CREATE TABLE ipc_commits (
  realm_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  operation_key TEXT NOT NULL,
  commit_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  state_version INTEGER NOT NULL,
  committed_at TEXT NOT NULL,
  PRIMARY KEY (realm_id,store_id,epoch,operation_key),
  UNIQUE (commit_id),
  UNIQUE (realm_id,store_id,epoch,request_id)
);
CREATE TABLE ipc_grant_consumption (
  realm_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  grant_id TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  request_id TEXT NOT NULL,
  consumed_at TEXT NOT NULL,
  PRIMARY KEY (realm_id,store_id,epoch,grant_id)
);
```
This is a **minimal schema**, not a migration for the production ledger. The engine must not allow `PENDING` to become a claim that an external effect occurred. If `ADMIT` and `COMMIT` share a grant, explicitly model separate grants or allowed grant lifecycle; do not silently reuse a single-use grant. A `COMMIT` must reference a previously persisted authorized admission and expected state version, checked under the same transaction. Revocation/epoch/quarantine checks must be synchronized with their authoritative updates, not performed only before `BEGIN`.

## Canonical request JSON (illustrative)
```json
{
  "protocol": "tenir.g1.ipc.v1",
  "message_type": "REQUEST",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "operation": "COMMIT",
  "realm_id": "realm-test",
  "store_id": "store-test",
  "epoch": 12,
  "operation_key": "invoice-17",
  "idempotency_key": "client-attempt-17",
  "payload_digest": "sha256:<64-lowercase-hex>",
  "payload": {"admission_receipt_id": "receipt-17", "expected_state_version": 4},
  "grant": {
    "grant_id": "grant-17",
    "issuer_key_id": "issuer-test",
    "subject": "worker-principal-1",
    "realm_id": "realm-test",
    "store_id": "store-test",
    "epoch": 12,
    "operation_key": "invoice-17",
    "allowed_operation": "COMMIT",
    "payload_digest": "sha256:<64-lowercase-hex>",
    "not_before": "2026-10-25T18:00:00Z",
    "expires_at": "2026-10-25T18:05:00Z",
    "signature": "<base64url-detached>"
  }
}
```
Placeholders above are **not** valid digests/signatures. Canonicalization, strict JSON duplicate-key rejection, signature domain separation, signature suite, trusted issuer mapping and clock tolerance must be specified and tested before accepting grants. The digest is computed over the canonical payload bytes and checked against the signed grant; the engine derives peer identity from `SO_PEERCRED` and maps it to the grant subject. Client-provided `subject` is not authentication.

## Transaction and replay algorithm
1. Decode bounded length-prefixed frame; reject malformed/oversized/duplicate-key input before mutation. Derive kernel peer principal.
2. Authenticate grant issuer and subject; verify signature, scope, payload digest, expiry and current policy.
3. `BEGIN IMMEDIATE` on the engine-owned SQLite connection; serialize the authoritative revocation/quarantine/epoch check with the commit point (or fail closed when the authority is not coherently available).
4. Lookup `ipc_requests` by full scoped idempotency primary key. If present and immutable request fingerprint matches, return its persisted result; if any immutable field differs, return `CONFLICT`. Do not let expired/revoked grants initiate a new transition merely because an old receipt is retrievable; `STATUS` retrieval has independent authorization.
5. For new `COMMIT`: check persisted admission, expected state version, no existing `ipc_commits` for the operation, and unused single-use grant; insert request, grant consumption, commit and durable receipt within **one SQLite transaction**.
6. `COMMIT` transaction; only then respond `COMMITTED`. If process crashes or response is lost, client calls authorized `STATUS` (or retries exact request) to recover the durable result.
7. Any exception after SQLite commit may produce an **ambiguous caller-visible result**; never report `committed:false` unless rollback is known. `STATUS` must distinguish committed from unknown.
8. Conflict between an exact replay and a newly revoked authorization: do not perform new work; allow read-only receipt lookup only through authorized STATUS, with audit trail.

## Response JSON
```json
{
  "protocol": "tenir.g1.ipc.v1",
  "message_type": "RESPONSE",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "decision": "COMMITTED",
  "reason_code": "DURABLE_LOCAL_COMMIT",
  "epoch": 12,
  "state_version": 5,
  "committed": true,
  "receipt_digest": "sha256:<64-lowercase-hex>",
  "retryable": false
}
```
Allowed decisions: `ADMITTED`, `COMMITTED`, `HOLD`, `DENIED`, `UNKNOWN`, `CONFLICT`, `CANCELLED`. `committed` is `true`, `false`, or `null`; `null` denotes unknown. Never equate a socket timeout with no commit. Persisted response must be deterministic, with mutable transport diagnostics outside the signed/hashed receipt.

## Prototype acceptance vectors
- T1: worker UID cannot open DB for writing or replace DB/WAL/SHM/sidecars; verify directory and file permission mechanism, not merely missing temp file.
- T2: engine UID accepts authorized IPC `ADMIT` + `COMMIT`; precisely one local commit row and durable receipt.
- T3: concurrent identical `COMMIT` requests return same receipt; one commit row.
- T4: same idempotency key with changed payload/operation scope → `CONFLICT`, zero additional commits.
- T5: grant reissued for same operation cannot create second commit; different principal or epoch cannot bypass operation identity policy.
- T6: lost response after commit and daemon restart → `STATUS` retrieves committed state, no new transition.
- T7: revoke/epoch/quarantine race before commit point → HOLD; if ordering cannot be proven, fail closed.
- T8: unauthorized UID, malformed frames, forged signature, replayed/expired grants and raw SQL operation → rejected.
- T9: post-commit close failure → ambiguous caller outcome and durable recovery, not false rollback.
- T10: privileged storage-UID rollback and coherent sidecar rollback → outside preventive G1, requires independent CB-05; no G2 claim.

## Dependencies and gates
G1 POSIX sandbox adapter and daemon not implemented. Validate SQLite journaling mode and fsync assumptions, filesystem ACLs, socket credentials, grant signing and key custody. Run actual cross-UID and process crash tests, record logs/JUnit/SHA256. **No merge or production deployment.**
