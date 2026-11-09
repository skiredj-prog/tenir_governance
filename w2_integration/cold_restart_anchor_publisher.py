"""JW-10 cold-restart anchor *publication* protocol — laboratory scope.

This module drafts, at laboratory scope, the crash-safe publication protocol
named as JW-10 P0 exit criterion #2: publishing the Kernel+Witness checkpoint
to a third party across a window that can be interrupted between the Kernel
commit, the Witness commit, and the third party's acceptance.

What it IS
----------
- A *simulated* external monotone authority (``MonotoneAnchorAuthority``) that
  signs with an independent Ed25519 key and binds its monotonicity to the
  **Witness's own append-only chain**, not to a caller-chosen counter. It
  implements the ``CurrentAnchorReader`` protocol consumed by
  ``ExternalColdRestartAnchorGate``.
- A publication step (``publish_cold_restart_anchor``) that reads the Witness
  head revision and chain, fingerprints the two local SQLite files in a
  quiescent state, and classifies the outcome as ``APPLIED`` / ``IDEMPOTENT`` /
  ``HOLD`` / ``UNKNOWN_EXTERNAL``.

Store-bound monotonicity (closes the republication-laundering hole)
-------------------------------------------------------------------
An earlier version enforced anti-downgrade only on a caller-chosen generation
integer, so after a joint Kernel+Witness restore a caller could publish a
higher integer on the restored state and launder the rollback into the current
anchor. This is fixed by deriving the generation from the Witness head revision
(``witness_checkpoints.revision``) and requiring every publication to EXTEND the
anchored state: a strictly newer Witness head is accepted only if its chain
still contains the previously anchored checkpoint at the anchored revision;
anything that does not extend — a lower revision, a different checkpoint at the
anchored revision, or a fork — is HOLD. Recovery obeys the same rule.

What it is NOT
--------------
- It does NOT make the Kernel commit, the Witness commit and third-party
  acceptance a single cross-domain atomic transaction. No such atomicity is
  claimed. The three commits remain distinct and a crash can land between any
  two of them; recovery is characterised, not prevented.
- The authority is an in-memory/local-file stand-in, NOT a deployed, live,
  independently operated monotone service with distinct access control. A
  reader that violates freshness (replays a validly-signed old generation)
  defeats it; signature alone is not freshness. **And a fully compromised host
  holding the Witness signing key can still fabricate an extending chain** —
  that is the out-of-scope compromised-writer case, not closed here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from enum import Enum

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from w2_integration.external_cold_restart_anchor import (
    SCHEMA,
    ExternalAnchorHold,
    sqlite_logical_fingerprint,
    _canonical,
)

_HEX = set("0123456789abcdef")


class PublicationOutcome(str, Enum):
    """Classification of a cold-restart anchor publication attempt."""

    APPLIED = "APPLIED"              # a strictly newer generation was accepted
    IDEMPOTENT = "IDEMPOTENT"        # same generation + identical digests: no-op
    HOLD = "HOLD"                    # anti-downgrade / equivocation refused
    UNKNOWN_EXTERNAL = "UNKNOWN_EXTERNAL"  # third party never acknowledged


class AnchorEquivocation(ExternalAnchorHold):
    """Same revision presented with a different Witness head: HOLD, never silent."""


class AnchorDoesNotExtend(ExternalAnchorHold):
    """A newer revision whose chain drops the anchored checkpoint: HOLD."""


@dataclass(frozen=True)
class PublicationResult:
    outcome: PublicationOutcome
    subject: str
    generation: int          # == Witness head revision, store-derived
    kernel_sha256: str
    witness_sha256: str
    envelope: dict | None
    witness_head_fingerprint: str | None = None


def _witness_head(witness_db: str) -> tuple[int, str | None]:
    """Return (revision, head snapshot_fingerprint) of the Witness chain head."""
    try:
        with closing(sqlite3.connect(witness_db)) as db:
            db.row_factory = sqlite3.Row
            has = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='witness_checkpoints'"
            ).fetchone()
            if not has:
                return 0, None
            row = db.execute(
                "SELECT revision, record_json FROM witness_checkpoints "
                "ORDER BY revision DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise ExternalAnchorHold("cannot read Witness chain head") from exc
    if row is None:
        return 0, None
    try:
        fingerprint = json.loads(row["record_json"])["snapshot_fingerprint"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ExternalAnchorHold("Witness chain head record invalid") from exc
    return int(row["revision"]), fingerprint


def _witness_fingerprint_at(witness_db: str, revision: int) -> str | None:
    """Return the chain's snapshot_fingerprint at an exact revision, or None."""
    try:
        with closing(sqlite3.connect(witness_db)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT record_json FROM witness_checkpoints WHERE revision=?",
                (revision,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ExternalAnchorHold("cannot read Witness chain") from exc
    if row is None:
        return None
    try:
        return json.loads(row["record_json"])["snapshot_fingerprint"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ExternalAnchorHold("Witness chain record invalid") from exc


def _kernel_event_rows(kernel_db: str) -> list[bytes]:
    """Canonical per-row bytes of the append-only Kernel event log, in id order.

    ``w2_positive_v1_events`` is append-only (its UPDATE/DELETE triggers abort),
    so this ordered list is a monotone Kernel position. A Kernel-only restore
    truncates it; a fork diverges its prefix. It is the ordering signal the
    whole-database digest lacks.
    """
    try:
        with closing(sqlite3.connect(kernel_db)) as db:
            db.row_factory = sqlite3.Row
            has = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='w2_positive_v1_events'"
            ).fetchone()
            if not has:
                return []
            rows = db.execute(
                "SELECT id, operation_key, event, at, detail "
                "FROM w2_positive_v1_events ORDER BY id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ExternalAnchorHold("cannot read Kernel event log") from exc
    return [
        _canonical({
            "id": row["id"], "operation_key": row["operation_key"],
            "event": row["event"], "at": row["at"], "detail": row["detail"],
        })
        for row in rows
    ]


def _kernel_prefix_hash(rows: list[bytes], length: int) -> str:
    """Hash chain over the first ``length`` ordered Kernel event rows."""
    digest = hashlib.sha256()
    digest.update(b"TENIR-JW10-KERNEL-EVENT-PREFIX-v1")
    for row in rows[:length]:
        digest.update(len(row).to_bytes(8, "big"))
        digest.update(row)
    return digest.hexdigest()


class MonotoneAnchorAuthority:
    """Simulated third-party monotone authority with an independent key.

    Durability model is laboratory-only: the accepted high-water mark lives in
    process memory (or is reconstructed by the caller). This stands in for a
    deployed monotone service; it does not provide its availability, its
    distinct host/access boundary, or protection against an operator who can
    roll the authority's own state back (see ``test_jw15_oracles`` for that
    residual on the sibling anchor).
    """

    def __init__(self, private_key: Ed25519PrivateKey | None = None):
        self._key = private_key or Ed25519PrivateKey.generate()
        self._lock = threading.Lock()
        self._current: dict[str, dict] = {}

    @property
    def public_key_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def _sign(self, payload: dict) -> dict:
        return {"payload": payload,
                "signature_hex": self._key.sign(_canonical(payload)).hex()}

    def publish(self, *, subject: str, topology_id: str, kernel_db: str,
                witness_db: str) -> PublicationResult:
        """Publish the quiescent stores, binding monotonicity to the Witness chain.

        The generation is the Witness head revision read from ``witness_db`` — a
        caller cannot choose it. Acceptance rules (``current`` = the anchored
        checkpoint for this subject):

        - no current anchor yet -> ``APPLIED`` (bootstrap);
        - Witness revision < anchored revision -> HOLD (anti-downgrade);
        - Witness revision == anchored revision:
            * different Witness head fingerprint -> HOLD (equivocation);
            * identical digests -> ``IDEMPOTENT``;
            * same Witness head, advanced Kernel digest -> ``APPLIED``
              (Kernel-only progress within the same Witness epoch, e.g. admit /
              send events that do not mint a checkpoint);
        - Witness revision > anchored revision:
            * the chain's fingerprint at the anchored revision must still equal
              the anchored Witness head fingerprint (EXTENSION) -> ``APPLIED``;
            * otherwise -> HOLD (does not extend / fork).

        Kernel axis (always, once anchored). Witness checkpoints are minted only
        on claim, so admit and send are Kernel-only progress within one Witness
        epoch; the whole-database digest does not order Kernel states. To stop a
        Kernel-only restore from laundering through the same-revision branch, the
        append-only Kernel event log must EXTEND the anchored prefix: its length
        must not shrink and its first ``anchored_count`` rows must hash
        identically. A shorter or diverging log is HOLD, whatever the revision.
        """
        if not isinstance(subject, str) or not subject:
            raise ExternalAnchorHold("publication subject required")
        revision, head_fingerprint = _witness_head(witness_db)
        kernel_sha256 = sqlite_logical_fingerprint(kernel_db)
        witness_sha256 = sqlite_logical_fingerprint(witness_db)
        for digest in (kernel_sha256, witness_sha256):
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in _HEX for char in digest)):
                raise ExternalAnchorHold("publication digest invalid")
        kernel_rows = _kernel_event_rows(kernel_db)
        kernel_count = len(kernel_rows)
        kernel_prefix = _kernel_prefix_hash(kernel_rows, kernel_count)
        payload = {
            "schema": SCHEMA,
            "subject": subject,
            "topology_id": topology_id,
            "generation": revision,
            "kernel_sha256": kernel_sha256,
            "witness_sha256": witness_sha256,
        }
        with self._lock:
            current = self._current.get(subject)
            if current is not None:
                cur = current["envelope"]["payload"]
                cur_head = current["witness_head_fingerprint"]
                cur_kernel_count = current["kernel_event_count"]
                cur_kernel_prefix = current["kernel_event_prefix"]
                # Witness axis.
                if revision < cur["generation"]:
                    raise ExternalAnchorHold(
                        "external monotone anchor anti-downgrade: HOLD")
                if revision == cur["generation"]:
                    if head_fingerprint != cur_head:
                        raise AnchorEquivocation(
                            "external monotone anchor Witness equivocation at revision")
                else:
                    anchored_at = _witness_fingerprint_at(witness_db, cur["generation"])
                    if anchored_at != cur_head:
                        raise AnchorDoesNotExtend(
                            "external monotone anchor: new state does not extend the "
                            "anchored Witness checkpoint: HOLD")
                # Kernel axis (orders admit/send progress within an epoch).
                if kernel_count < cur_kernel_count:
                    raise ExternalAnchorHold(
                        "external monotone anchor Kernel anti-downgrade: HOLD")
                if _kernel_prefix_hash(kernel_rows, cur_kernel_count) != cur_kernel_prefix:
                    raise AnchorDoesNotExtend(
                        "external monotone anchor: Kernel event log does not extend "
                        "the anchored prefix: HOLD")
                if (cur["kernel_sha256"] == kernel_sha256
                        and cur["witness_sha256"] == witness_sha256
                        and cur["topology_id"] == topology_id):
                    return PublicationResult(
                        PublicationOutcome.IDEMPOTENT, subject, revision,
                        kernel_sha256, witness_sha256,
                        current["envelope"], head_fingerprint)
                # Not idempotent, yet the append-only Kernel event log did not
                # grow: a Kernel change that minted no event. Nothing orders
                # such a change, so a rollback is indistinguishable from
                # progress -- refuse it as a Kernel equivocation. Every native
                # writer that mutates durable state appends an event, so this
                # branch is reachable only by an unanchored/event-less mutation
                # (see bind_b1_dual_envelope, which now appends one).
                if kernel_count == cur_kernel_count:
                    raise AnchorEquivocation(
                        "external monotone anchor: Kernel digest changed with no new "
                        "event at an unchanged event position: HOLD")
            envelope = self._sign(payload)
            self._current[subject] = {
                "envelope": envelope,
                "witness_head_fingerprint": head_fingerprint,
                "kernel_event_count": kernel_count,
                "kernel_event_prefix": kernel_prefix,
            }
            return PublicationResult(
                PublicationOutcome.APPLIED, subject, revision,
                kernel_sha256, witness_sha256, envelope, head_fingerprint)

    # --- CurrentAnchorReader protocol -------------------------------------
    def read_current(self, subject: str) -> dict:
        with self._lock:
            current = self._current.get(subject)
        if current is None:
            raise OSError("external monotone anchor has no current checkpoint")
        return json.loads(json.dumps(current["envelope"]))  # defensive copy


def quiescent_fingerprints(authority) -> tuple[str, str]:
    """Fingerprint the Kernel and Witness SQLite files of a native authority.

    Requires a quiescent (no in-flight native write) single-host context; this
    is a read, not a cross-database atomic snapshot.
    """
    return (
        sqlite_logical_fingerprint(authority.db_path),
        sqlite_logical_fingerprint(authority.witness.db_path),
    )


def publish_cold_restart_anchor(
    authority, publisher: MonotoneAnchorAuthority, *, subject: str,
    _crash_before_acceptance: bool = False,
) -> PublicationResult:
    """Read the quiescent local stores and publish to the third party.

    The generation is store-derived (the Witness head revision); no caller
    counter is accepted. The crash-safety contract is characterised, not
    enforced by atomicity:

    - The Kernel and Witness commits have already happened (separately) by the
      time this runs; this step publishes their *joint* state.
    - ``_crash_before_acceptance`` models a process kill after the local
      commits but before the third party acknowledges. It returns
      ``UNKNOWN_EXTERNAL`` without advancing the authority: the local state is
      then ahead of the anchor, and recovery MUST re-run this step. Because
      ``publish`` is idempotent on an unchanged state and extend-only otherwise,
      that recovery is safe; a downgrade or non-extending state still HOLDs.
    """
    if _crash_before_acceptance:
        kernel_sha256, witness_sha256 = quiescent_fingerprints(authority)
        revision, _ = _witness_head(authority.witness.db_path)
        return PublicationResult(
            PublicationOutcome.UNKNOWN_EXTERNAL, subject, revision,
            kernel_sha256, witness_sha256, None)
    return publisher.publish(
        subject=subject,
        topology_id=authority.topology_id,
        kernel_db=authority.db_path,
        witness_db=authority.witness.db_path,
    )
