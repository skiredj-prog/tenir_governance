"""EP-02 external high-water authority contract (experimental, pure).

This module compares authenticated checkpoints and refuses stale/unavailable
anchor states. It does NOT implement an anchor server, durable CAS or commit.
"""
from dataclasses import dataclass
from enum import Enum
import re

from tenir_governance.experimental.ep02_rotation import TrustCheckpoint


class AnchorStatus(str, Enum):
    CURRENT = "CURRENT"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class AnchorObservation:
    status: AnchorStatus
    checkpoint: TrustCheckpoint | None


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _valid_checkpoint(value):
    return (
        isinstance(value, TrustCheckpoint)
        and type(value.revision) is int
        and value.revision >= 1
        and isinstance(value.snapshot_fingerprint, str)
        and _HEX64.fullmatch(value.snapshot_fingerprint) is not None
    )


def assess_anchor(
    local: TrustCheckpoint,
    observation: AnchorObservation,
) -> tuple[bool, str]:
    """Fail closed on missing, inconsistent or newer remote high-water marks."""
    if not _valid_checkpoint(local):
        return False, "LOCAL_CHECKPOINT_INVALID"
    if observation.status != AnchorStatus.CURRENT or observation.checkpoint is None:
        return False, "ANCHOR_UNAVAILABLE_OR_CONFLICT"
    remote = observation.checkpoint
    if not _valid_checkpoint(remote):
        return False, "ANCHOR_CHECKPOINT_INVALID"
    if remote.revision > local.revision:
        return False, "LOCAL_CHECKPOINT_STALE"
    if remote.revision < local.revision:
        return False, "ANCHOR_BEHIND_LOCAL"
    if remote.snapshot_fingerprint != local.snapshot_fingerprint:
        return False, "CHECKPOINT_EQUIVOCATION"
    return True, "CHECKPOINTS_AGREE"
