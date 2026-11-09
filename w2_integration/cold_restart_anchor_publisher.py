"""JW-10 cold-restart anchor *publication* protocol — laboratory scope.

This module drafts, at laboratory scope, the crash-safe publication protocol
named as JW-10 P0 exit criterion #2: publishing the Kernel+Witness checkpoint
to a third party across a window that can be interrupted between the Kernel
commit, the Witness commit, and the third party's acceptance.

What it IS
----------
- A *simulated* external monotone authority (``MonotoneAnchorAuthority``) that
  signs with an independent Ed25519 key, enforces per-subject anti-downgrade,
  and is idempotent on re-publication of an identical generation. It implements
  the ``CurrentAnchorReader`` protocol consumed by
  ``ExternalColdRestartAnchorGate``.
- A publication step (``publish_cold_restart_anchor``) that fingerprints the
  two local SQLite files in a quiescent state and classifies the outcome as
  ``APPLIED`` / ``IDEMPOTENT`` / ``HOLD`` / ``UNKNOWN_EXTERNAL``.

What it is NOT
--------------
- It does NOT make the Kernel commit, the Witness commit and third-party
  acceptance a single cross-domain atomic transaction. No such atomicity is
  claimed. The three commits remain distinct and a crash can land between any
  two of them; recovery is characterised, not prevented.
- The authority is an in-memory/local-file stand-in, NOT a deployed, live,
  independently operated monotone service with distinct access control. A
  reader that violates freshness (replays a validly-signed old generation)
  defeats it; signature alone is not freshness.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from enum import Enum

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from external_cold_restart_anchor import (
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
    """Same generation presented with different digests: HOLD, never silent."""


@dataclass(frozen=True)
class PublicationResult:
    outcome: PublicationOutcome
    subject: str
    generation: int
    kernel_sha256: str
    witness_sha256: str
    envelope: dict | None


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

    def publish(self, *, subject: str, topology_id: str, generation: int,
                kernel_sha256: str, witness_sha256: str) -> PublicationResult:
        """Accept a strictly-newer checkpoint; refuse a downgrade; be idempotent.

        A generation already present with identical digests returns
        ``IDEMPOTENT`` (safe recovery/retry). The same generation with different
        digests is an equivocation and HOLDs. A strictly older generation HOLDs
        (anti-downgrade). This is the only place the authority advances.
        """
        if not isinstance(subject, str) or not subject:
            raise ExternalAnchorHold("publication subject required")
        if type(generation) is not int or generation < 1:
            raise ExternalAnchorHold("publication generation must be a positive int")
        for digest in (kernel_sha256, witness_sha256):
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in _HEX for char in digest)):
                raise ExternalAnchorHold("publication digest invalid")
        payload = {
            "schema": SCHEMA,
            "subject": subject,
            "topology_id": topology_id,
            "generation": generation,
            "kernel_sha256": kernel_sha256,
            "witness_sha256": witness_sha256,
        }
        with self._lock:
            current = self._current.get(subject)
            if current is not None:
                cur = current["payload"]
                if generation < cur["generation"]:
                    raise ExternalAnchorHold(
                        "external monotone anchor anti-downgrade: HOLD")
                if generation == cur["generation"]:
                    if (cur["kernel_sha256"] != kernel_sha256
                            or cur["witness_sha256"] != witness_sha256
                            or cur["topology_id"] != topology_id):
                        raise AnchorEquivocation(
                            "external monotone anchor equivocation at generation")
                    return PublicationResult(
                        PublicationOutcome.IDEMPOTENT, subject, generation,
                        kernel_sha256, witness_sha256, current)
            envelope = self._sign(payload)
            self._current[subject] = envelope
            return PublicationResult(
                PublicationOutcome.APPLIED, subject, generation,
                kernel_sha256, witness_sha256, envelope)

    # --- CurrentAnchorReader protocol -------------------------------------
    def read_current(self, subject: str) -> dict:
        with self._lock:
            current = self._current.get(subject)
        if current is None:
            raise OSError("external monotone anchor has no current checkpoint")
        return json.loads(json.dumps(current))  # defensive copy


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
    generation: int, _crash_before_acceptance: bool = False,
) -> PublicationResult:
    """Fingerprint the quiescent local stores and publish to the third party.

    The crash-safety contract is characterised, not enforced by atomicity:

    - The Kernel and Witness commits have already happened (separately) by the
      time this runs; this step publishes their *joint* fingerprint.
    - ``_crash_before_acceptance`` models a process kill after the local
      commits but before the third party acknowledges. It returns
      ``UNKNOWN_EXTERNAL`` without advancing the authority: the local state is
      then ahead of the anchor, and recovery MUST re-run this step. Because
      ``publish`` is idempotent on an identical generation, that recovery is
      safe; a downgrade attempt during recovery still HOLDs.
    """
    kernel_sha256, witness_sha256 = quiescent_fingerprints(authority)
    if _crash_before_acceptance:
        return PublicationResult(
            PublicationOutcome.UNKNOWN_EXTERNAL, subject, generation,
            kernel_sha256, witness_sha256, None)
    return publisher.publish(
        subject=subject,
        topology_id=authority.topology_id,
        generation=generation,
        kernel_sha256=kernel_sha256,
        witness_sha256=witness_sha256,
    )
