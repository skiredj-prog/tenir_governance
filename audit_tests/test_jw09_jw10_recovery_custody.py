"""JW-09/JW-10: real witness/kernel recovery on pinned JW-08 parent."""
import os
import sqlite3
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.independent_witness import IndependentWitness
from w2_integration.native_positive_permit import NativePositivePermitAuthority


def _keys(monkeypatch, *, kernel=bytes(range(32)), witness=bytes(range(32,64))):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX",kernel.hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX",witness.hex())


def test_jw10_ephemeral_rejected_on_verifiable_path(tmp_path,monkeypatch):
    monkeypatch.delenv("TENIR_WITNESS_PRIVATE_KEY_HEX",raising=False)
    monkeypatch.delenv("TENIR_W2_PRIVATE_KEY_HEX",raising=False)
    with pytest.raises(RuntimeError,match="injected witness key"):
        IndependentWitness(str(tmp_path/"w.sqlite"),verifiable_recovery=True)
    backend=SQLiteConsensusBackend(str(tmp_path/"budget"))
    with pytest.raises(RuntimeError,match="injected witness key"):
        NativePositivePermitAuthority(backend,verifiable_recovery=True)


def test_jw10_restart_same_keys_pass_and_changed_witness_fails(tmp_path,monkeypatch):
    _keys(monkeypatch)
    db=str(tmp_path/"witness.sqlite")
    before=IndependentWitness(db,verifiable_recovery=True)
    assert IndependentWitness(db,verifiable_recovery=True).key_id==before.key_id
    _keys(monkeypatch,witness=bytes(range(64,96)))
    with pytest.raises(RuntimeError,match="custody key changed"):
        IndependentWitness(db,verifiable_recovery=True)


def test_jw10_kernel_change_rejected_on_restart(tmp_path,monkeypatch):
    _keys(monkeypatch)
    backend=SQLiteConsensusBackend(str(tmp_path/"budget"))
    original=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    assert NativePositivePermitAuthority(backend,verifiable_recovery=True).key_id==original.key_id
    _keys(monkeypatch,kernel=bytes(range(96,128)))
    with pytest.raises(RuntimeError,match="kernel/witness key changed"):
        NativePositivePermitAuthority(backend,verifiable_recovery=True)


def test_jw09_orphan_checkpoint_reuses_same_execution_and_claim_identity(tmp_path,monkeypatch):
    _keys(monkeypatch)
    w=IndependentWitness(str(tmp_path/"witness.sqlite"),verifiable_recovery=True)
    first=w.issue("op:idem","ab"*32,identity={"execution_id":"exec-original","claim_token":"token-original"})
    assert w.verify(first,subject="op:idem")
    recovered=IndependentWitness(str(tmp_path/"witness.sqlite"),verifiable_recovery=True)
    # New proposal is not a new authorization: old durable identity must win.
    replay=recovered.issue("op:idem","ab"*32,identity={"execution_id":"exec-NEW","claim_token":"token-NEW"})
    assert replay==first
    assert replay["record"]["execution_id"]=="exec-original"
    assert replay["record"]["claim_token"]=="token-original"
    with sqlite3.connect(str(tmp_path/"witness.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM witness_checkpoints").fetchone()[0]==1


def test_jw09_foreign_subject_and_command_hash_refused(tmp_path,monkeypatch):
    _keys(monkeypatch)
    w=IndependentWitness(str(tmp_path/"w.sqlite"),verifiable_recovery=True)
    a=w.issue("op-A:idem","ab"*32,identity={"execution_id":"exec-A","claim_token":"token-A"})
    assert not w.verify(a,subject="op-B:idem")
    with pytest.raises(RuntimeError,match="already resolved"):
        w.issue("op-A:idem","cd"*32,identity={"execution_id":"other","claim_token":"other"})
    assert w.verify(a,subject="op-A:idem")


def test_jw09_legacy_checkpoint_without_binding_not_upgraded(tmp_path,monkeypatch):
    _keys(monkeypatch)
    db=str(tmp_path/"w.sqlite")
    w=IndependentWitness(db,verifiable_recovery=True)
    old=w.issue("op:idem","ab"*32)
    assert w.verify(old,subject="op:idem")
    with pytest.raises(RuntimeError,match="missing immutable identity"):
        w.issue("op:idem","ab"*32,identity={"execution_id":"ex","claim_token":"tok"})
