# G1-IPC v0.1 — Worker ↔ Storage Engine protocol
Status: **DESIGN ONLY / NOT IMPLEMENTED / NOT TESTED / PRODUCTION HOLD**
Scope: POSIX/Linux UNIX-domain socket on disposable G1 sandbox; no raw SQL access.

## Trust boundary and principals
- Worker UID/GID differs from engine/storage UID/GID. Worker cannot write or replace SQLite, WAL/SHM, JSON sidecars, or their parent directory.
- Engine owns persistent state and exposes a restricted IPC socket in a separate directory with controlled traverse/connect permissions.
- On each accepted connection the engine obtains kernel peer credentials (`SO_PEERCRED` on Linux); never trust `caller_uid` in payload as authentication. Revalidate process identity against a configured authorization mapping. Beware inherited file descriptors, shared UID, privilege changes and proxy processes.
- For local IPC, kernel peer credentials + socket permissions provide channel-level peer identity; signed grants provide scoped authorization. An HMAC shared with the worker does not prevent a compromised worker from signing arbitrary requests, so do not treat it as an additional privilege boundary.

## Transport and framing
- UNIX `SOCK_STREAM`, length-prefixed canonical UTF-8 JSON frames: 4-byte unsigned big-endian length followed by exactly that many bytes; maximum 64 KiB per frame, configurable downward. Reject oversized, truncated, duplicate-key, malformed or non-canonical payloads. No path arguments, SQL fragments or arbitrary method dispatch.
- One authenticated connection may carry multiple requests, but each request is authorized independently. Enforce per-peer concurrency and rate limits; do not rely on socket path secrecy.
- Deadline is relative to engine's monotonic clock after reception; caller-supplied timestamps are not proof of freshness.

## Request envelope (proposed schema)
```json
{
  "protocol": "tenir.g1.ipc.v1",
  "message_type": "REQUEST",
  "request_id": "uuid",
  "operation": "ADMIT",
  "realm_id": "realm",
  "store_id": "store",
  "epoch": 12,
  "operation_key": "stable-domain-key",
  "idempotency_key": "stable-client-key",
  "payload_digest": "sha256:...",
  "payload": {},
  "grant": {
    "grant_id": "unique-id",
    "issuer_key_id": "engine-authority-key-id",
    "subject": "bound-worker-principal",
    "realm_id": "realm",
    "store_id": "store",
    "epoch": 12,
    "operation_key": "stable-domain-key",
    "allowed_operation": "ADMIT",
    "payload_digest": "sha256:...",
    "not_before": "RFC3339",
    "expires_at": "RFC3339",
    "signature": "base64url-detached"
  }
}
```
Grant signatures cover a canonical encoding of all grant fields except signature, with explicit protocol/domain separation. Signature suite, canonicalization, key lifecycle and clock-skew policy are design choices to finalize. Peer identity is matched to grant subject using a trusted engine-side UID/principal mapping; client-supplied subject alone is insufficient. The engine checks current revocation/epoch and the operation's domain policy before mutation.

## Allowed operations and authority
- `ADMIT`: evaluate/admit a uniquely scoped operation; returns an authorized receipt or HOLD, not raw SQL access.
- `COMMIT`: consume a previously issued, single-use, operation-bound authorization and persist the permitted transition; must bind to the same operation, payload, realm, epoch and state/version. This does not imply an external provider effect.
- `STATUS`: read durable status by scoped request ID/idempotency key, with authorization; resolves uncertain outcomes after response loss.
- `CANCEL`: request cancellation only while the operation has not crossed the irreversible commit boundary; never falsely claim reversal of committed work.
No arbitrary `EXEC_SQL`, `ATTACH`, path, DDL, grant-minting or quarantine-release operation is available to the worker.

## Engine authorization order
1. Decode and validate bounded frame; reject duplicate keys, unsupported versions/operations and invalid digests.
2. Obtain kernel peer credentials and map to authorized principal and realm.
3. Verify signed grant, issuer trust, revocation, epoch, scope, operation, payload hash, subject, time bounds and current policy.
4. Resolve idempotency against durable state: exact retry returns existing result; same key with different payload/scope is a conflict; unknown outcome yields explicit HOLD/UNKNOWN, never a new effect by default.
5. Validate current quarantine/authority state and expected operation version **under the storage transaction's appropriate locks**; prevent TOCTOU.
6. Commit allowed state transition and durable evidence atomically within the declared local storage boundary; only then send success response.
7. Log security denials and status without exposing secrets or private keys.

## Response envelope
`protocol`, `message_type: RESPONSE`, `request_id`, `decision: ADMITTED|HOLD|DENIED|UNKNOWN|CONFLICT`, `reason_code`, `epoch`, `state_version`, `receipt_digest`, `committed: true|false|null`, `retryable`. `committed:null` explicitly represents uncertainty. Never equate transport error or HOLD with non-commit. Authenticated response signatures are optional for in-host IPC but required for externally portable evidence; define separate receipt signing contract.

## Security invariants
- G1-I01: no worker write access to storage filesystem; kernel denies direct SQLite/sidecar mutation.
- G1-I02: IPC operation allowlist; no arbitrary SQL/DDL/path.
- G1-I03: signed grant is bound to authenticated peer, realm/store, epoch, operation, payload and request identity; cross-context replay denied.
- G1-I04: revocation/epoch checked at the transaction linearization point; concurrent revocation semantics must be tested.
- G1-I05: durable at-most-once state transition per scoped idempotency key; ambiguous outcomes resolved by STATUS.
- G1-I06: malformed or unauthenticated input fails closed without mutation.
- G1-I07: quarantine/authority state checked under a coherent lock boundary; external CB-05 witness remains a separate assurance gate.

## Negative tests (adapter extension)
N01 unprivileged worker opens SQLite directly → kernel denial, no mutation.
N02 worker replaces SQLite/WAL/SHM or quarantine sidecar → kernel denial, no mutation.
N03 unauthorized UID connects to socket → rejected.
N04 worker sends `EXEC_SQL` or DDL payload → rejected.
N05 tampered grant signature / wrong principal / wrong realm / wrong epoch / payload mismatch → rejected.
N06 exact replay returns same durable receipt; divergent replay → CONFLICT.
N07 revoke races COMMIT → deterministic linearization; no post-revocation commit.
N08 lost COMMIT response → STATUS returns committed/unknown without second mutation.
N09 truncated/oversized/duplicate-key frames → rejected before mutation.
N10 compromised worker with valid grant tries supervisor-only quarantine release → denied.
N11 engine crash before/after local COMMIT → recovery without double settlement.
N12 same-UID or root attacker modifies DB directly → explicitly outside preventive G1 isolation; detect via CB-05 only under independent witness assumptions.

## Deployment/test gates
Separate engine and worker OS identities, filesystem ownership evidence, real peer-credential assertions, adapter integration, negative tests with actual logs/JUnit/SHA-256, crash and concurrency cases. No claim that socket-only authentication protects against root/storage-UID compromise. No code changes to G4-A in this specification. **PRODUCTION HOLD**.
