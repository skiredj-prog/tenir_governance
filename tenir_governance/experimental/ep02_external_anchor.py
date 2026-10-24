"""EP-02 external high-water authority contract (experimental, pure).

This module compares authenticated checkpoints and refuses stale/unavailable
anchor states. It does NOT implement an anchor server, durable CAS or commit.
"""
from dataclasses import dataclass
from enum import Enum

from tenir_governance.experimental.ep02_rotation import TrustCheckpoint


class AnchorStatus(str, Enum):
    CURRENT = "CURRENT"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class AnchorObservation:
    status: AnchorStatus
    checkpoint: TrustCheckpoint | None


def assess_anchor(
    local: TrustCheckpoint,
    observation: AnchorObservation,
) -> tuple[bool, str]:
    """Fail closed on missing, inconsistent or newer remote high-water marks."""
    if observation.status != AnchorStatus.CURRENT or observation.checkpoint is None:
        return False, "ANCHOR_UNAVAILABLE_OR_CONFLICT"
    remote = observation.checkpoint
    if remote.revision > local.revision:
        return False, "LOCAL_CHECKPOINT_STALE"
    if remote.revision < local.revision:
        return False, "ANCHOR_BEHIND_LOCAL"
    if remote.snapshot_fingerprint != local.snapshot_fingerprint:
        return False, "CHECKPOINT_EQUIVOCATION"
    return True, "CHECKPOINTS_AGREE"
