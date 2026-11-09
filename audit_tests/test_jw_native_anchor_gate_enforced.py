"""JW-10: the cold-restart anti-rollback gate ENFORCED on native paths.

These tests exercise ``NativePositivePermitAuthority.attach_cold_restart_anchor_gate``
so the anchor check actually gates native admission and send, and — with a
publisher attached — a gated admit -> claim -> send runs end-to-end with no
manual republication. They prove positive enforcement, the anti-rollback
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
    "resource_id": "lab:budget", "amount": 3, "action_type": "payment",
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
    assert backend.register_resource("lab:budget", 90) == "REGISTERED"
    return backend, NativePositivePermitAuthority(backend, verifiable_recovery=True)


def _bootstrap(authority, publisher):
    """Establish a Witness revision-1 chain and anchor it, before gating."""
    authority.admit_reserve_issue("op0", "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim("op0", PAYLOAD, "op0-idem")
    result = publish_cold_restart_anchor(authority, publisher, subject=SUBJECT)
    assert result.outcome is PublicationOutcome.APPLIED and result.generation == 1


def _gate(publisher, *, topology=TOPOLOGY_ID):
    return ExternalColdRestartAnchorGate(
        reader=publisher, trusted_public_key=publisher.public_key_bytes,
        required_topology=topology)


def test_gated_admit_claim_send_runs_end_to_end_without_manual_republish(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    _bootstrap(authority, publisher)
    authority.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)

    permit = authority.admit_reserve_issue("op1", "eval", PAYLOAD, resource_id="lab:budget")
    assert permit["permit_id"] == "permit-op1"
    claim = authority.consume_and_claim("op1", PAYLOAD, "op1-idem")
    assert claim["claimed"]
    # No manual publish between claim and send: the native paths re-anchor.
    assert authority.grant_send_right(claim) == "GRANTED"
    assert authority._last_anchor_publication.outcome in (
        PublicationOutcome.APPLIED, PublicationOutcome.IDEMPOTENT)


def test_native_admission_refused_after_joint_rollback_with_live_reader(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    _bootstrap(authority, publisher)
    rev1_kernel = tmp_path / "rev1_kernel.sqlite3"
    rev1_witness = tmp_path / "rev1_witness.sqlite3"
    _snapshot(backend._database_path, rev1_kernel)
    _snapshot(authority.witness.db_path, rev1_witness)

    authority.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)
    # Advance one full gated flow; the live authority advances with it.
    authority.admit_reserve_issue("op1", "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim("op1", PAYLOAD, "op1-idem")

    # Attacker restores BOTH stores to the revision-1 state; the live reader
    # still holds the newer checkpoint.
    _snapshot(rev1_kernel, backend._database_path)
    _snapshot(rev1_witness, authority.witness.db_path)
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
    _bootstrap(authority, publisher)
    stale_envelope = publisher.read_current(SUBJECT)  # validly signed revision 1
    rev1_kernel = tmp_path / "rev1_kernel.sqlite3"
    rev1_witness = tmp_path / "rev1_witness.sqlite3"
    _snapshot(backend._database_path, rev1_kernel)
    _snapshot(authority.witness.db_path, rev1_witness)

    authority.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)
    authority.admit_reserve_issue("op1", "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim("op1", PAYLOAD, "op1-idem")

    # Joint rollback to revision 1.
    _snapshot(rev1_kernel, backend._database_path)
    _snapshot(rev1_witness, authority.witness.db_path)
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)

    # Control: a LIVE reader HOLDs.
    restarted.attach_cold_restart_anchor_gate(_gate(publisher), subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="rollback or drift"):
        restarted.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id="lab:budget")

    # A STALE reader replaying the signed revision-1 checkpoint is ACCEPTED,
    # because its digests match the rolled-back stores: rollback UNDETECTED.
    stale_gate = ExternalColdRestartAnchorGate(
        reader=_ReplayReader(stale_envelope),
        trusted_public_key=publisher.public_key_bytes,
        required_topology=TOPOLOGY_ID)
    restarted.attach_cold_restart_anchor_gate(stale_gate, subject=SUBJECT)
    accepted = restarted.admit_reserve_issue(
        "op2", "eval", PAYLOAD, resource_id="lab:budget")
    assert accepted["permit_id"] == "permit-op2"  # KNOWN GAP: signature != freshness


def test_kernel_only_restore_cannot_launder_a_second_send(tmp_path, monkeypatch):
    """Finding 3, end-to-end: after a granted send, restoring ONLY the Kernel to
    its pre-send state must not let a second send through — neither the gate nor
    extend-only recovery may accept the rolled-back Kernel."""
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    _bootstrap(authority, publisher)
    authority.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)

    authority.admit_reserve_issue("op1", "eval", PAYLOAD, resource_id="lab:budget")
    claim = authority.consume_and_claim("op1", PAYLOAD, "op1-idem")
    pre_send_kernel = tmp_path / "pre_send_kernel.sqlite3"
    _snapshot(backend._database_path, pre_send_kernel)  # post-claim, pre-send
    assert authority.grant_send_right(claim) == "GRANTED"

    # Restore ONLY the Kernel to its pre-send state; Witness untouched.
    _snapshot(pre_send_kernel, backend._database_path)
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    restarted.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)
    # The gate refuses the send outright (committed Kernel drifted from anchor).
    with pytest.raises(ExternalAnchorHold, match="rollback or drift"):
        restarted.grant_send_right(claim)
    # And extend-only recovery cannot launder the rolled-back Kernel either.
    with pytest.raises(ExternalAnchorHold, match="Kernel anti-downgrade"):
        publish_cold_restart_anchor(restarted, publisher, subject=SUBJECT)


def test_settlement_is_gated_by_the_cold_restart_anchor(tmp_path, monkeypatch):
    """R1: mark_dispatch_confirmed (I8 settlement) is now gated, so a rolled-back
    Kernel/Witness is refused before the effect is settled."""
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    _bootstrap(authority, publisher)
    boot_kernel = tmp_path / "boot_kernel.sqlite3"
    boot_witness = tmp_path / "boot_witness.sqlite3"
    _snapshot(backend._database_path, boot_kernel)
    _snapshot(authority.witness.db_path, boot_witness)
    authority.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)
    # Advance so the live anchor moves past the bootstrap snapshot.
    authority.admit_reserve_issue("op1", "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim("op1", PAYLOAD, "op1-idem")
    # Joint rollback to the bootstrap state; the live reader holds the newer one.
    _snapshot(boot_kernel, backend._database_path)
    _snapshot(boot_witness, authority.witness.db_path)
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    restarted.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)
    # Settlement HOLDs at the gate, before any receipt work.
    with pytest.raises(ExternalAnchorHold, match="rollback or drift"):
        restarted.mark_dispatch_confirmed(
            {"execution_id": "x", "claim_token": "y"}, {"k": "v"})


def test_settlement_gate_allows_current_state_then_validates_receipt(tmp_path, monkeypatch):
    """The settlement gate does not block the current state: it passes the gate
    and the inner path then rejects the bogus receipt (not an anchor HOLD)."""
    _keys(monkeypatch)
    backend, authority = _new_authority(tmp_path)
    publisher = MonotoneAnchorAuthority()
    _bootstrap(authority, publisher)
    authority.attach_cold_restart_anchor_gate(
        _gate(publisher), subject=SUBJECT, publisher=publisher)
    with pytest.raises(RuntimeError) as exc:
        authority.mark_dispatch_confirmed(
            {"execution_id": "x", "claim_token": "y"}, {"k": "v"})
    assert not isinstance(exc.value, ExternalAnchorHold)
    assert "provider receipt" in str(exc.value)


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
    _bootstrap(authority, publisher)
    prod_gate = _gate(publisher, topology="LAB-TENIR-GOV-PROD-CANDIDATE-09")
    authority.attach_cold_restart_anchor_gate(prod_gate, subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="identity, topology"):
        authority.admit_reserve_issue("op1", "eval", PAYLOAD, resource_id="lab:budget")
