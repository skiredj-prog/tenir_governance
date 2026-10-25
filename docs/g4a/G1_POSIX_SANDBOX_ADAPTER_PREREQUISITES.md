# G1 — POSIX sandbox isolation adapter prerequisites
Version 0.1 | Design/provisioning contract | **NO EXECUTION EVIDENCE / PRODUCTION HOLD**

## Scope
Provision an ephemeral POSIX/Linux test environment for `tests/test_g1_isolation.py`. This document does not assert that G1 is implemented. Do not count skipped, xfailed, or mocked tests as security PASS.

## Security identities
- `tenir-agent`: unprivileged application/execution UID; may access only the narrow authenticated governor IPC endpoint.
- `tenir-storage`: distinct UID owning SQLite database, `-wal`, `-shm`, directory and quarantine JSON/sentinel/lock files. Only this principal writes persistent authority state.
- `tenir-test-controller`: test harness/orchestrator, privileged only to set up and tear down disposable environment; never a normal application identity.
- Optional `tenir-auditor`: read-only, no storage write, no privileged IPC credentials.

UID separation alone is insufficient if the agent shares a writable directory, supplementary storage GID, container mount, ambient capabilities, namespace escape, or a privileged socket that accepts arbitrary SQL.

## Filesystem and runtime
- Disposable local POSIX filesystem; record `findmnt -T`, `stat -f -c %T`, mount flags and SQLite PRAGMAs (`journal_mode`, `synchronous`).
- Directory and all SQLite/sidecar artifacts writable only by storage principal. Agent has no write access to parent directory (prevents `os.replace` even when DB mode is read-only).
- Include WAL/SHM, JSON source, quarantine mark, initialized sentinel, release-pending and lock files; deny agent direct create/rename/unlink of any of them.
- Explicitly exclude shared UID, privileged containers, root, CAP_DAC_OVERRIDE, CAP_DAC_READ_SEARCH and host bind mounts from the conformant threat model; run these as separate privileged negative controls.
- Use dedicated test-only temp root and test-only accounts; do not modify host production users, chmod production paths, or use a live database.

## IPC authority
- Local Unix socket may be used, but pathname secrecy is not an access control. Enforce OS permissions **and** peer credentials (`SO_PEERCRED` on Linux) or equivalent authenticated identity; bind operation, realm, epoch, request_id, scope and idempotency key to the authorization decision.
- Storage service MUST expose only constrained domain operations, not raw SQL or arbitrary file paths.
- Authenticate and authorize every request, including retries; use a single-use or replay-safe grant protocol and explicit revocation epoch semantics.
- Audit denied requests. Automatic quarantine is only claimed when reliable detection and its atomic ordering are demonstrated.

## Adapter fixture required by test_g1_isolation.py
Implement `isolation_adapter` in a separate test-only module. Expose:
`db_path`, `sidecar_paths`, `agent_uid`, `storage_uid`, `invoke_as_agent(argv)`, `authorized_mutation(operation_key)`, `read_state()`, `grant(operation_key, epoch)`, `submit_grant(grant, operation_key, epoch)`, `revoke_epoch()`.
`invoke_as_agent` must really execute with the agent's UID/GID and no storage capabilities; assert identity from inside the child. `read_state` must cover SQLite and relevant sidecars (not merely an unrelated byte string). `authorized_mutation` must traverse authenticated IPC and change a controlled state.

## Required tests and oracles
- G1-01: direct SQLite CREATE/UPDATE by agent fails **before commit**, persistent state unchanged.
- G1-02: `os.replace`, unlink, create and overwrite of SQLite, WAL/SHM, source, quarantine sidecars denied; verify post-test state and ownership.
- G1-03: grant rejected across operation, epoch, realm and process identity; replay same grant rejected unless protocol explicitly supports bound idempotent replay.
- G1-04: revoked grant rejected before mutation; concurrent revoke/commit race must have a specified linearization point.
- G1-05: positive control authorized domain operation succeeds through IPC and produces correct durable evidence.
- G1-06: same-UID/privileged writer negative control demonstrates boundary limitations and is recorded as expected compromise, not conformant PASS.
- Additional mandatory negative vectors: symlink/hardlink attacks, SQLite ATTACH, malicious path input, Unix socket peer spoofing, inherited FD, WAL/SHM tampering, concurrent requests and restart/recovery.

## Important test-harness caveats
The initial `test_g1_isolation.py` is a **contract scaffold**. Its `isolation_adapter` fixture calls `pytest.skip`, and its privileged-writer vector `pytest.xfail`; no execution guarantee exists. A real adapter must override the fixture and produce evidence. For file-replacement tests, failure to create the attacker's temporary file alone does not demonstrate resistance to `os.replace`; add controls proving the attempted primitive reached the intended boundary. Compare canonical SQLite state and file hashes, not raw database bytes alone when WAL is active.

## Evidence package
Record exact SUT SHA, adapter commit, `id -a` for each identity, `stat` and ACLs of directories/files, `findmnt`, `uname`, Python/SQLite versions, test commands, stdout/stderr, exit codes, actual JUnit, SHA-256 manifest, before/after state and whether attack reached boundary. Separate `pytest PASS` from `SECURITY PASS`. Use independent reviewer.

## Gates
G1 is not demonstrated until real cross-UID OS isolation is provisioned, negative attacks are rejected before commit, positive control works, and adversarial review accepts raw evidence. A compromised storage UID/root remains outside preventive OS isolation; CB-05 independent witness addresses detection under its own explicit trust assumptions.

**PRODUCTION HOLD — no merge, deployment, or production permissions change.**
