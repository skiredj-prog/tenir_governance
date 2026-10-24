"""EP-02 experimental trust sequence validation.

Requires caller supplied authenticated snapshots and an independently
persistent minimum revision. This module performs no irreversible execution.
"""
from dataclasses import dataclass

from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState, verify_bound_snapshot,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from typing import Mapping


@dataclass(frozen=True)
class TrustCheckpoint:
    revision: int
    snapshot_fingerprint: str


def validate_successor(
    previous: TrustCheckpoint,
    candidate: SignedTrustState,
    *, roots: Mapping[str, Ed25519PublicKey],
) -> tuple[bool, str, TrustCheckpoint | None]:
    """Require monotone revision; rotation must occur at a newer revision."""
    from hashlib import sha256
    from tenir_governance.experimental.ep02_signed_trust import bound_snapshot_bytes
    if not verify_bound_snapshot(candidate, root_keys=roots,
                                 minimum_revision=previous.revision):
        return False, "UNVERIFIED_OR_ROLLBACK", None
    new_digest = sha256(bound_snapshot_bytes(candidate)).hexdigest()
    revision = candidate.state.revision
    if revision == previous.revision:
        if new_digest == previous.snapshot_fingerprint:
            return True, "UNCHANGED", previous
        return False, "SAME_REVISION_EQUIVOCATION", None
    if revision < previous.revision:
        return False, "REVISION_ROLLBACK", None
    return True, "ADVANCED", TrustCheckpoint(revision, new_digest)
