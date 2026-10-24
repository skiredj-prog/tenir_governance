# EP-02 trust security boundaries

Status: experimental security qualification. No governor or production runtime
integration is authorized by this document.

## Canonical trust inputs

Trust revisions are positive exact integers; booleans are rejected. Policy,
mandate, revocation, signer and issuer identities are bounded nonempty strings.
Duplicate mandates, revocations and issuer keys fail closed. Snapshot digests
are lowercase SHA-256 hex. V2 issuer key entries use exact 32-byte Ed25519
public keys and are covered by the root signature.

Malformed values are rejected before canonicalization, signature verification
or authority mutation. This removes Python coercions such as `True == 1` and
prevents distinct caller objects from acquiring one accepted canonical form.

## Legacy v1 boundary

The v1 `verified_preflight` path accepts issuer keys separately from the signed
trust snapshot. It therefore cannot prove that the root authorized those keys.
It now fails closed by default with `LEGACY_UNBOUND_ISSUER_KEYS_DISABLED`.

Compatibility use requires the explicit
`allow_legacy_unbound_issuer_keys=True` acknowledgment on both preflight and
revalidation. This flag documents the weaker assumption; it does not make the
keys root-bound. New work must use `verified_bound_preflight` and
`revalidate_bound`, where v2 issuer keys are part of the root-signed payload.

## Challenge freshness

`new_anchor_challenge()` and `new_lookup_challenge()` generate 256-bit random
challenges. Each challenge is single use from the caller's perspective. A
verified signature authenticates the response to that challenge; the pure
verification helpers do not maintain a global replay database. Predictable or
reused challenges weaken freshness even when signatures remain valid.

Anchor inputs require a bounded identity, exact positive revision, lowercase
SHA-256 fingerprint and a 16 to 64 byte challenge. A receipt for one challenge
does not validate against another.

## Local storage trust boundary

The durable SQLite authority rejects symlinked database, WAL and shared-memory
paths and provisions the database without group or world access on POSIX. It
checks the authority profile on each new connection, requires immutable-row
triggers, and replays state-changing history on open.

These controls detect malformed, accidentally replaced and partially edited
local evidence. They do not defend against a privileged administrator who can
replace the database, WAL, executable code, root profile and every local copy
with a mutually consistent older version.

## Root and key lifecycle

The durable authority pins a root profile ID, generation and public key set at
one-time provisioning. There is no in-place root replacement API. Reusing a
profile generation with different keys is outside the contract and fails
identity validation when the persisted profile changes.

Issuer key rotation is expressed inside a newer v2 root-signed snapshot and is
subject to monotone CAS. Same-revision key substitution is equivocation. Root
key rotation, client authorization key rotation and service signing key
rotation require a separately authenticated operator procedure and a new
profile generation. This repository does not yet claim that operational
procedure.

## Anti-rollback qualification

A remote monotone authority can reveal that a restored local checkpoint is
stale. The experimental external CAS protocol provides authenticated writes
and reads to one central authority. It does not make the authority itself
immune to a consistent rollback.

Protection from rollback of the authority and all its local trust material
requires an independent monotone witness, hardware backed counter, append-only
external transparency log, or consensus system whose failure assumptions are
documented and tested. Local hashes and signatures prove integrity and
authenticity of the restored bytes; they do not prove those bytes are latest.
