# Agent 2 / B1 — R-01 same-UID adversarial boundary, POSIX separate-UID lab

**Status:** test-only Draft. **SUT exact baseline:** `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3` (G4-A PR #73). No implementation or provider code modified. The test branch inherits the SUT and adds only tests, raw evidence generator, workflow and this document. It is **not** a Source Lock/Integration Lock on PR #103/#109.

## Trust boundary and threat model

The current laboratory G4-A uses connection-local SQLite `tenir_g4a_write` SQL UDF checks, schema guards, and reopen validation. These are not mandatory OS-level restrictions for code with write access to `consensus.sqlite3`.

**Disposal-only Linux lab topology:** a trusted **writer process** runs under EUID/GID 61021; an untrusted **agent/connector process** runs under EUID/GID 61022; a privileged test harness provisions both via Linux `setgroups([])`, `setgid`, `setuid`. Writer state is owned by 61021 behind a directory with POSIX 0700 permissions. The parent test controller uses root to switch identities, but it never signs, grants or dispatches a provider call. Tenir's receipts remain HOLD, `execution_authorized=false`.

A root principal, `CAP_DAC_OVERRIDE`, a namespace allowing UID remapping, malicious code in the writer EUID, direct raw privileged SQLite/DDL, file replacement, and distributed/multi-host actors are NOT fenced by POSIX 0700. An actual service isolation and least-privilege deployment is still required. This lab does not demonstrate a deployed independent writer daemon or authenticated RPC boundary.

## R01-N01..N06 RED-first vector policy

| Vector | Test | Security verdict expected on baseline |
|---|---|---|
| N01 | Same-UID ordinary direct SQL resources update with no UDF | `PASS` if denied before commit |
| N02 | Same-UID ordinary direct SQL outbox mutation with no UDF | `PASS` if denied before commit |
| N03 | Same-UID UDF `tenir_g4a_write` spoof, budget update and reopen | **`FAIL`** if already committed; later detection is not prevention |
| N04 | Same-UID trigger DDL removal and update | **`FAIL`** if already committed |
| N05 | Same-UID SQLite inode replacement | **`FAIL`** if replacement succeeds, even if content is identical |
| N06 | Independently inspect alternative writer paths (backend `_connect`/`_transaction`, sqlite3/UDF, file replacement) | **`FAIL`** for broad exclusive-writer claims |

**B1 OS boundary:** repeat ordinary SQL, UDF spoof, DDL, direct new SQL insert, file replacement, Source JSON, G3 marker, Witness location and native open as EUID 61022. Expect **permission denials with no durable mutation**. Confirm EUID 61021 can inspect its state with a HOLD receipt; confirm a hostile actor who regains EUID 61021 can still exploit UDF. Do not count test assertions passing as successful privileged security.

The test oracle hashes **all SQLite schema objects and table rows** before/after each vector, captures process JSON/stdout/stderr, EUID, owner/mode, and machine-vector `PASS/FAIL`. A machine-report verifier refuses missing vectors or a false PASS assigned to a committed privileged bypass. JUnit and raw console text are separately archived and SHA-256 indexed.

## Operational follow-up after test evidence

1. Build an actual **separate writer service** with a restricted IPC/socket and no shared EUID or writable mount from agents/connectors.
2. Deny `SQLITE_OPEN_READWRITE`, writable FS/ACL, `dlopen` and DDL to caller processes. Establish reproducible mandatory writer route, not optional Python wrapper.
3. Inventory all workers, API processes, SDKs, privileged administration/recovery, file/DB backups, G3 locks, Witness and external anchor stores. Explicitly fence unpatched legacy writers.
4. Re-execute identical negative cases against the deployed **converged** SHA/topology; source convergence PR #109 and the independent custody/high-water tickets #105/#106 are distinct prerequisites. Have Agent 3 independently replay, then PMO decide Integration Lock.

**No merge or production promotion**: Issue #74 remains P0 until an actual separated writer boundary and independent review establish the full requested guarantee. Draft PR #73 and PRs #103/#109 are untouched.
