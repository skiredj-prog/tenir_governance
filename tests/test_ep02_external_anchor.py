"""External checkpoint authority availability and disagreement characterization."""
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_external_anchor import (
    AnchorObservation, AnchorStatus, assess_anchor,
)

LOCAL = TrustCheckpoint(2, "a" * 64)


def test_matching_anchor_is_usable_only_as_preflight():
    assert assess_anchor(LOCAL, AnchorObservation(AnchorStatus.CURRENT, LOCAL)) == (
        True, "CHECKPOINTS_AGREE")


def test_anchor_unavailable_fails_closed():
    assert assess_anchor(LOCAL, AnchorObservation(AnchorStatus.UNAVAILABLE, None)) == (
        False, "ANCHOR_UNAVAILABLE_OR_CONFLICT")


def test_remote_newer_detects_restored_local_checkpoint():
    remote = TrustCheckpoint(3, "b" * 64)
    assert assess_anchor(LOCAL, AnchorObservation(AnchorStatus.CURRENT, remote)) == (
        False, "LOCAL_CHECKPOINT_STALE")


def test_anchor_behind_local_is_not_silently_accepted():
    remote = TrustCheckpoint(1, "b" * 64)
    assert assess_anchor(LOCAL, AnchorObservation(AnchorStatus.CURRENT, remote)) == (
        False, "ANCHOR_BEHIND_LOCAL")


def test_equal_revision_different_digest_is_conflict():
    remote = TrustCheckpoint(2, "b" * 64)
    assert assess_anchor(LOCAL, AnchorObservation(AnchorStatus.CURRENT, remote)) == (
        False, "CHECKPOINT_EQUIVOCATION")


def test_reported_conflict_fails_closed():
    assert assess_anchor(LOCAL, AnchorObservation(AnchorStatus.CONFLICT, LOCAL)) == (
        False, "ANCHOR_UNAVAILABLE_OR_CONFLICT")
