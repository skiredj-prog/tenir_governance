"""Independent anchor contract tests: demonstrate scope AND stale-reader limit.

The anchor signer is independent from Kernel and Witness. The reader is an
in-memory laboratory stand-in, NOT a real network monotonic service.
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_positive_permit import NativePositivePermitAuthority, TOPOLOGY_ID
from w2_integration.external_cold_restart_anchor import (
    SCHEMA, ExternalColdRestartAnchorGate, ExternalAnchorHold,
    sqlite_logical_fingerprint, _canonical,
)


class LabLiveCurrentAnchor:
    def __init__(self, current=None):
        self.current=current
        self.available=True
    def read_current(self, subject):
        if not self.available:
            raise OSError("independent signer unreachable")
        return deepcopy(self.current)


def _backup(src,dst):
    with sqlite3.connect(str(src)) as first,sqlite3.connect(str(dst)) as next_:
        first.backup(next_)


def _signed(priv,backend,witness,*,generation=1,subject="lab:checkpoint",topology=TOPOLOGY_ID):
    payload={
        "schema":SCHEMA,"subject":subject,"topology_id":topology,
        "generation":generation,
        "kernel_sha256":sqlite_logical_fingerprint(backend._database_path),
        "witness_sha256":sqlite_logical_fingerprint(witness.witness.db_path),
    }
    return {"payload":payload,"signature_hex":priv.sign(_canonical(payload)).hex()}


@pytest.fixture
def candidate(tmp_path,monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX",bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX",bytes(range(32,64)).hex())
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    assert backend.register_resource("lab:budget",20)=="REGISTERED"
    native=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    payload={
        "schema":"tenir-dispatch-command/v1",
        "resource_id":"lab:budget","amount":7,
        "action_type":"payment","agent_id":"lab-agent",
        "metadata":{"recipient":"lab-only"},
    }
    native.admit_reserve_issue(
        "lab:anchor-op","lab:eval",payload,resource_id="lab:budget",
    )
    anchor_signer=Ed25519PrivateKey.generate()  # separate trust domain in test
    anchor_pub=anchor_signer.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    reader=LabLiveCurrentAnchor()
    gate=ExternalColdRestartAnchorGate(
        reader=reader,trusted_public_key=anchor_pub,required_topology=TOPOLOGY_ID)
    return tmp_path,backend,native,payload,anchor_signer,reader,gate


def test_trusted_current_anchor_rejects_joint_kernel_witness_rollback(candidate):
    tmp,backend,native,payload,signer,reader,gate=candidate
    pre_kernel=tmp/"pre_kernel.sqlite3"
    pre_witness=tmp/"pre_witness.sqlite3"
    _backup(backend._database_path,pre_kernel)
    _backup(native.witness.db_path,pre_witness)
    old_envelope=_signed(signer,backend,native,generation=1)
    claim=native.consume_and_claim(
        "lab:anchor-op",payload,"lab:idem")
    assert claim["claimed"]
    reader.current=_signed(signer,backend,native,generation=2)
    assert reader.current["payload"]["kernel_sha256"]==sqlite_logical_fingerprint(backend._database_path), (
        "kernel fingerprint changed between signing and immediate checking")
    assert reader.current["payload"]["witness_sha256"]==sqlite_logical_fingerprint(native.witness.db_path), (
        "witness fingerprint changed between signing and immediate checking")
    approved=gate.verify_cold_restart(
        subject="lab:checkpoint",kernel_db=backend._database_path,
        witness_db=native.witness.db_path,min_generation=2)
    assert approved.generation==2
    assert approved.execution_authorized is False
    assert approved.normative_verified is False

    _backup(pre_kernel,backend._database_path)
    _backup(pre_witness,native.witness.db_path)
    # The independent LIVE reader retains the newer signed generation 2.
    with pytest.raises(ExternalAnchorHold,match="rollback or drift"):
        gate.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path,min_generation=2)

    # CRITICAL NEGATIVE CONTROL: if the external reader itself can replay a
    # valid old generation, cryptography alone cannot establish freshness.
    reader.current=old_envelope
    old=gate.verify_cold_restart(
        subject="lab:checkpoint",kernel_db=backend._database_path,
        witness_db=native.witness.db_path)
    assert old.generation==1
    with pytest.raises(ExternalAnchorHold,match="generation mismatch"):
        gate.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path,min_generation=2)


def test_anchor_not_optional_when_invoked_and_reader_outage_holds(candidate):
    _,backend,native,_,signer,reader,gate=candidate
    reader.current=_signed(signer,backend,native)
    reader.available=False
    with pytest.raises(ExternalAnchorHold,match="unavailable"):
        gate.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path)
    with pytest.raises(ExternalAnchorHold,match="reader required"):
        ExternalColdRestartAnchorGate(
            reader=None,trusted_public_key=bytes(32),
            required_topology=TOPOLOGY_ID)


def test_wrong_anchor_key_or_tampered_record_refused(candidate):
    _,backend,native,_,signer,reader,gate=candidate
    reader.current=_signed(signer,backend,native)
    another=Ed25519PrivateKey.generate()
    other_key=another.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    wrong=ExternalColdRestartAnchorGate(
        reader=reader,trusted_public_key=other_key,
        required_topology=TOPOLOGY_ID)
    with pytest.raises(ExternalAnchorHold,match="signature invalid"):
        wrong.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path)
    reader.current["payload"]["generation"]=99
    with pytest.raises(ExternalAnchorHold,match="signature invalid"):
        gate.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path)


def test_anchor_subject_topology_and_snapshot_changes_hold(candidate):
    _,backend,native,_,signer,reader,gate=candidate
    reader.current=_signed(signer,backend,native)
    with pytest.raises(ExternalAnchorHold,match="identity, topology"):
        gate.verify_cold_restart(
            subject="foreign",kernel_db=backend._database_path,
            witness_db=native.witness.db_path)
    other=ExternalColdRestartAnchorGate(
        reader=reader,trusted_public_key=signer.public_key().public_bytes(
            Encoding.Raw,PublicFormat.Raw),
        required_topology="LAB-TENIR-GOV-PROD-CANDIDATE-09",
    )
    with pytest.raises(ExternalAnchorHold,match="identity, topology"):
        other.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path)
    with backend._connection() as db:
        db.execute("CREATE TABLE lab_new_drift(marker TEXT)")
    with pytest.raises(ExternalAnchorHold,match="rollback or drift"):
        gate.verify_cold_restart(
            subject="lab:checkpoint",kernel_db=backend._database_path,
            witness_db=native.witness.db_path)


def test_independent_current_anchor_is_cold_restart_only_not_dispatch_permission(candidate):
    _,backend,native,payload,signer,reader,gate=candidate
    reader.current=_signed(signer,backend,native)
    assert reader.current["payload"]["kernel_sha256"]==sqlite_logical_fingerprint(backend._database_path), "kernel fingerprint drift on stable read"
    assert reader.current["payload"]["witness_sha256"]==sqlite_logical_fingerprint(native.witness.db_path), "witness fingerprint drift on stable read"
    receipt=gate.verify_cold_restart(
        subject="lab:checkpoint",kernel_db=backend._database_path,
        witness_db=native.witness.db_path)
    assert receipt.state=="COLD_RESTART_MATCHED_ONLY"
    assert receipt.execution_authorized is False
    assert not hasattr(gate,"dispatch")
    assert not hasattr(gate,"commit")
    assert not hasattr(gate,"publish")
