"""JW-10: the cold-restart anti-rollback gate ENFORCED on native paths.

These tests exercise ``NativePositivePermitAuthority.attach_cold_restart_anchor_gate``
so the anchor check actually gates native admission and send, rather than being
a standalone verifier. They prove the positive enforcement, the anti-rollback
refusal against a LIVE monotone reader, and — mandatory — the documented
negative freshness control: a stale/compromised reader replaying a validly
signed OLD checkpoint is ACCEPTED on the native path, because a signature alone
is not freshness. Full closure needs a genuinely external live monotone
authority and the prod-candidate topology (kept non-matching here).
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_positive_permit import NativePositivePermitAuthority, TOPOLOGY_ID
from w2_integration.external_cold_restart_anchor import (
    ExternalColdRestartAnchorGate,
    ExternalAnchorHold,
)
from w2_integration.cold_restart_anchor_publisher import (
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


class _ReplayReader:
    """In-memory reader that always returns one fixed envelope (or an outage)."""

    def __init__(self, envelope=None, *, available=True):
        self.envelope = envelope
        self.available = available

    def read_current(self, subject):
        if not self.available:
            raise OSError("independent signer unreachable")
        return deepcopy(self.envelope)


def _snapshot(src, dst):
    with sqlite3.connect(str(src)) as source, sqlite3.connect(str(dst)) as dest:
        source.backup(dest)


def _keys(monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())


def _new_authority(tmp_path):
    backend = SQLiteConsensusBackend(str(tmp_path / "kernel"))
    assert backend.register_resource("lab:budget", 20) == "REGISTERED"
    return backend, NativePositivePermitAuthority(backend, verifiable_recovery=True)


def _publish_current(publisher, authority, generation):
    result = publish_cold_restart_anchor(
        authority, publisher, subject=SUBJECT, generation=generation)
    assert result.outcome in (PublicationOutcome.APPLIED, PublicationOutcome.IDEMPOTENT)
    return result


def _gate(publisher, *, topology=TOPOLOGY_ID):
    return ExternalColdRestartAnchorGate(
        reader=publisher, trusted_public_key=publisher.public_key_bytes,
        required_topology=topology)


def test_native_admission_and_send_pass_when_anchor_is_current(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    # Anchor the quiescent pre-admission state, then enforce the gate.
    _publish_current(publisher, authority, generation=1)
    authority.attach_cold_restart_anchor_gate(_gate(publisher), subject=SUBJECT)

    permit = authority.admit_reserve_issue(
        "op", "eval", PAYLOAD, resource_id="lab:budget")
    assert permit["permit_id"] == "permit-op"

    claim = authority.consume_and_claim("op", PAYLOAD, "idem")
    assert claim["claimed"]
    # Re-publish the post-claim committed state so the send-path gate matches.
    _publish_current(publisher, authority, generation=2)
    assert authority.grant_send_right(claim) == "GRANTED"


def test_native_admission_refused_after_joint_rollback_with_live_reader(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    pre_kernel = tmp_path / "pre_kernel.sqlite3"
    pre_witness = tmp_path / "pre_witness.sqlite3"
    _snapshot(backend._database_path, pre_kernel)
    _snapshot(authority.witness.db_path, pre_witness)

    _publish_current(publisher, authority, generation=1)
    authority.attach_cold_restart_anchor_gate(_gate(publisher), subject=SUBJECT)
    authority.admit_reserve_issue("op", "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim("op", PAYLOAD, "idem")
    # LIVE monotone authority advances to the post-claim generation/fingerprint.
    _publish_current(publisher, authority, generation=2)

    # Attacker restores BOTH stores to the pre-claim state. The live reader
    # still holds generation 2 with the NEWER fingerprint.
    _snapshot(pre_kernel, backend._database_path)
    _snapshot(pre_witness, authority.witness.db_path)
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    restarted.attach_cold_restart_anchor_gate(_gate(publisher), subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="rollback or drift"):
        restarted.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id="lab:budget")


def test_native_paths_accept_stale_reader_replay_is_a_freshness_gap(tmp_path, monkeypatch):
    """MANDATORY NEGATIVE CONTROL — documented, never papered over.

    A stale/compromised reader replaying a validly signed OLD checkpoint whose
    digests match the rolled-back stores is ACCEPTED by the native gate. The
    cryptographic signature proves authorship, not freshness. This is the
    irreducible residual that a real external live monotone authority — absent
    here — would close.
    """
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    pre_kernel = tmp_path / "pre_kernel.sqlite3"
    pre_witness = tmp_path / "pre_witness.sqlite3"
    _snapshot(backend._database_path, pre_kernel)
    _snapshot(authority.witness.db_path, pre_witness)

    # Capture a validly signed OLD (generation 1) envelope bound to the
    # pre-claim state, then advance the live authority to generation 2.
    stale_envelope = _publish_current(publisher, authority, generation=1).envelope
    authority.admit_reserve_issue("op", "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim("op", PAYLOAD, "idem")
    _publish_current(publisher, authority, generation=2)

    # Joint rollback to the pre-claim state.
    _snapshot(pre_kernel, backend._database_path)
    _snapshot(pre_witness, authority.witness.db_path)
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)

    # A LIVE reader (generation 2) would HOLD: control assertion.
    restarted.attach_cold_restart_anchor_gate(_gate(publisher), subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="rollback or drift"):
        restarted.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id="lab:budget")

    # A STALE reader replaying the signed generation-1 checkpoint is ACCEPTED,
    # because its digests match the rolled-back stores. The native admission
    # proceeds — freshness was NOT established by the signature alone.
    stale_gate = ExternalColdRestartAnchorGate(
        reader=_ReplayReader(stale_envelope),
        trusted_public_key=publisher.public_key_bytes,
        required_topology=TOPOLOGY_ID)
    restarted.attach_cold_restart_anchor_gate(stale_gate, subject=SUBJECT)
    accepted = restarted.admit_reserve_issue(
        "op2", "eval", PAYLOAD, resource_id="lab:budget")
    assert accepted["permit_id"] == "permit-op2"  # rollback undetected: KNOWN GAP


def test_native_path_holds_on_reader_outage(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    outage_gate = ExternalColdRestartAnchorGate(
        reader=_ReplayReader(None, available=False),
        trusted_public_key=bytes(32),
        required_topology=TOPOLOGY_ID)
    authority.attach_cold_restart_anchor_gate(outage_gate, subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="unavailable"):
        authority.admit_reserve_issue("op", "eval", PAYLOAD, resource_id="lab:budget")


def test_native_paths_unchanged_without_gate(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    assert authority._cold_restart_gate is None
    authority.admit_reserve_issue("op", "eval", PAYLOAD, resource_id="lab:budget")
    claim = authority.consume_and_claim("op", PAYLOAD, "idem")
    assert authority.grant_send_right(claim) == "GRANTED"


def test_prod_candidate_topology_remains_unverified(tmp_path, monkeypatch):
    """The requested prod-candidate topology is NOT matched: native paths HOLD."""
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    _publish_current(publisher, authority, generation=1)
    prod_gate = _gate(publisher, topology="LAB-TENIR-GOV-PROD-CANDIDATE-09")
    authority.attach_cold_restart_anchor_gate(prod_gate, subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="identity, topology"):
        authority.admit_reserve_issue("op", "eval", PAYLOAD, resource_id="lab:budget")
