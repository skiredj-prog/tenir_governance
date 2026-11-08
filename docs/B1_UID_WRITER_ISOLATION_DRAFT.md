# B1: separate the writer identity from workers

Status: design and acceptance criteria only. B1 remains OPEN. No production
activation or change to existing G4-A and positive authority is implied.

## Problem and trust boundary

The Linux R-01 campaign against subject
`f4072116404d403c82c5ae1acf2622e9e66af9a5` demonstrated that a process sharing
the writer UID can register a SQLite UDF, drop a trigger, or replace the
database inode. Application-level ownership checks cannot distinguish a
hostile process that has the same effective OS credentials as the writer.

The proposed boundary runs an authority service under a dedicated writer UID.
Workers use different, unprivileged UIDs and communicate through a local Unix
socket with peer credentials. The service owns the authority databases, WAL,
SHM, lock files, frozen registry, sentinel and epoch markers. The service's
UID, executable and state paths must not be writable by workers. No deployed
worker or legacy binary may retain the writer UID, root, capabilities allowing
DAC override, or another write path to the protected state. Socket access
alone must not confer a provider-dispatch capability.

Same-writer-UID compromise, root, CAP_DAC_OVERRIDE, hostile host administrators,
and multi-host fencing are outside this boundary. A separate independent
witness/custody boundary remains required; colocating a witness with this
service is not independent custody.

## Required implementation

1. Inventory and provision every live writer and protected path, including old
   JSON binaries, SQLite journal files, epoch/marker files and witness state.
2. Start the service only after checking its OS identity, protected-directory
   ownership, modes and parent-path integrity. Refuse symlinks, insecure or
   preexisting sockets, insecure paths, unsupported platforms and identity
   collisions. Specify operator-controlled installation and restart behavior.
3. Authenticate each Unix-socket peer using kernel-provided credentials and an
   explicit allowed UID policy. Bound request sizes, timeouts, types and command
   surface; reject malformed, unknown and unauthorized requests fail-closed.
4. Use a typed, versioned command for G4-A operations, preserving operation
   identity, transactional quarantine, epoch, replay/idempotence and HOLD.
   Never expose SQL, arbitrary file operations, Python callbacks, or the
   authority's signing keys over the socket. Validate every request before any
   mutation. Retain evidence for denied as well as accepted requests.
5. Integrate the actual native positive path and all supported SDK/worker entry
   points before claiming system-wide enforcement. Keep the historical G4-A
   `execution_authorized=false` and the positive laboratory path distinct.

## Acceptance — real Linux processes, exact candidate and topology

- A real writer service and worker run with different effective UIDs. An
  allowed worker performs an authorized simulated request through IPC; receipts
  remain explicitly synthetic and `execution_authorized=false`.
- An unallowed peer, same-writer-UID socket client and malformed or oversized
  request cannot cause a transition or reservation. Report request outcome,
  EUID/GID and pre/post hashes of database and protected side files.
- From the worker UID, attempt direct SQLite access, UDF spoofing, DDL, inode
  replacement, old JSON writes and marker changes. Each must be denied with no
  protected-state mutation. Check SQLite/WAL/SHM and directories separately.
- Exercise concurrent clients, activation/revocation, stale epochs, crash
  before/after commit, lost acknowledgment and restart. Assert one durable
  transition per idempotency key and preserve UNKNOWN on ambiguous outcomes.
- Run the existing R-01 same-UID counterexamples as negative controls; their
  expected ability to attack the writer-UID store must not be labelled PASS
  for same-UID protection. The fix is separation of untrusted workers from that
  UID, not prevention against hostile peers with the writer's own credentials.
- Run the above against the exact integrated candidate and deployment manifest,
  preserving raw results and distinguishing SUT, harness, and topology SHAs.

Until an implementation and these tests exist, B1, Integration Lock, REG+
and production remain HOLD.
