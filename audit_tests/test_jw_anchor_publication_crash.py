"""JW-10 exit criterion #2 — crash-safe anchor publication, CHARACTERISED.

The publication protocol spans three distinct commit points: the Kernel
commit, the Witness commit, and the third party's acceptance. This module
characterises the protocol's outcomes (APPLIED / IDEMPOTENT / HOLD /
UNKNOWN_EXTERNAL) and the idempotent recovery after a crash before third-party
acceptance. It does NOT claim cross-domain atomicity: the three commits remain
separate and a crash can land between any two of them. What is proven is that
recovery after such a crash is safe (idempotent re-publication) and that a
downgrade during recovery still HOLDs.
"""
from __future__ import annotations

import pytest

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_integration.cold_restart_anchor_publisher import (
    AnchorEquivocation,
    ExternalAnchorHold,
    MonotoneAnchorAuthority,
    PublicationOutcome,
    publish_cold_restart_anchor,
)

SUBJECT = "lab:cold-restart"
PAYLOAD = {
    "schema": "tenir-dispatch-command/v1",
    "resource_id": "lab:budget", "amount": 7, "action_type": "payment",
    "agent_id": "lab-agent", "metadata": {"recipient": "lab-no-effect"},
}


def _keys(monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())


def _authority(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend = SQLiteConsensusBackend(str(tmp_path / "kernel"))
    assert backend.register_resource("lab:budget", 20) == "REGISTERED"
    return backend, NativePositivePermitAuthority(backend, verifiable_recovery=True)


def _advance_local_state(authority, op):
    authority.admit_reserve_issue(op, "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim(op, PAYLOAD, f"{op}-idem")


def test_publication_applies_then_is_idempotent_on_identical_generation(tmp_path, monkeypatch):
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    first = publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)
    assert first.outcome is PublicationOutcome.APPLIED
    again = publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)
    assert again.outcome is PublicationOutcome.IDEMPOTENT
    assert again.kernel_sha256 == first.kernel_sha256
    assert again.witness_sha256 == first.witness_sha256


def test_publication_refuses_downgrade(tmp_path, monkeypatch):
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)
    _advance_local_state(authority, "op")
    assert publish_cold_restart_anchor(
        authority, publisher, subject=SUBJECT, generation=2).outcome is PublicationOutcome.APPLIED
    with pytest.raises(ExternalAnchorHold, match="anti-downgrade"):
        publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)


def test_publication_refuses_equivocation_at_same_generation(tmp_path, monkeypatch):
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)
    # Same generation, but the local stores changed: a different fingerprint at
    # an already-accepted generation is an equivocation and must HOLD.
    _advance_local_state(authority, "op")
    with pytest.raises(AnchorEquivocation, match="equivocation"):
        publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)


def test_crash_before_acceptance_is_unknown_external_then_recovers_idempotently(tmp_path, monkeypatch):
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)

    # Local Kernel+Witness commits land, but the process is killed before the
    # third party acknowledges the new generation.
    _advance_local_state(authority, "op")
    crashed = publish_cold_restart_anchor(
        authority, publisher, subject=SUBJECT, generation=2,
        _crash_before_acceptance=True)
    assert crashed.outcome is PublicationOutcome.UNKNOWN_EXTERNAL
    # The third party never advanced: local state is ahead of the anchor.
    assert publisher.read_current(SUBJECT)["payload"]["generation"] == 1

    # Recovery re-runs the identical publication; because the authority is
    # monotone and idempotent, this safely advances to generation 2 once.
    recovered = publish_cold_restart_anchor(
        authority, publisher, subject=SUBJECT, generation=2)
    assert recovered.outcome is PublicationOutcome.APPLIED
    assert publisher.read_current(SUBJECT)["payload"]["generation"] == 2
    # A replay of the same recovery is now idempotent, not a double advance.
    assert publish_cold_restart_anchor(
        authority, publisher, subject=SUBJECT, generation=2).outcome is PublicationOutcome.IDEMPOTENT


def test_downgrade_during_recovery_still_holds(tmp_path, monkeypatch):
    """A rollback attempt disguised as recovery is refused by anti-downgrade."""
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)
    _advance_local_state(authority, "op")
    publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=2)
    with pytest.raises(ExternalAnchorHold, match="anti-downgrade"):
        publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, generation=1)


def test_independent_anchor_key_is_distinct_from_kernel_and_witness(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    anchor_pub = publisher.public_key_bytes
    kernel_pub = authority.private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    witness_pub = authority.witness.private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    # A separate 32-byte Ed25519 identity, not the Kernel's or the Witness's.
    assert len(anchor_pub) == 32
    assert anchor_pub != kernel_pub
    assert anchor_pub != witness_pub
