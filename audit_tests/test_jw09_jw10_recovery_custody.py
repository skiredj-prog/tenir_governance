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


def _crash_between_witness_and_kernel_commit(directory):
    # Separate OS process: independent Witness COMMIT, then kernel kill.
    import os
    from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
    from w2_integration.native_positive_permit import NativePositivePermitAuthority
    backend=SQLiteConsensusBackend(directory)
    authority=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    backend._before_commit=lambda op: os._exit(86) if op=="positive_claim" else None
    command={
        "schema":"tenir-dispatch-command/v1",
        "resource_id":"lab:budget","amount":7,"action_type":"payment",
        "agent_id":"lab-agent","metadata":{"recipient":"lab-1"},
    }
    authority.consume_and_claim("lab:jw09",command,"lab:jw09-idem")
    os._exit(99)


def test_jw09_real_process_crash_leaves_one_witness_and_zero_kernel_outbox(
        tmp_path,monkeypatch):
    import multiprocessing
    _keys(monkeypatch)
    directory=str(tmp_path/"native")
    backend=SQLiteConsensusBackend(directory)
    assert backend.register_resource("lab:budget",20)=="REGISTERED"
    authority=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    payload={
        "schema":"tenir-dispatch-command/v1",
        "resource_id":"lab:budget","amount":7,"action_type":"payment",
        "agent_id":"lab-agent","metadata":{"recipient":"lab-1"},
    }
    authority.admit_reserve_issue(
        "lab:jw09","lab:evaluation",payload,resource_id="lab:budget",
    )
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_crash_between_witness_and_kernel_commit,
                      args=(directory,))
    child.start()
    child.join(timeout=20)
    assert child.exitcode==86
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0]==0
        assert db.execute(
            "SELECT consumed FROM w2_positive_v1_permits "
            "WHERE operation_key='lab:jw09'"
        ).fetchone()[0]==0
    witness_path=backend._database_path+".witness.sqlite3"
    with sqlite3.connect(witness_path) as db:
        before=db.execute(
            "SELECT record_json, signature FROM witness_checkpoints"
        ).fetchall()
    assert len(before)==1
    restored=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    claim=restored.consume_and_claim("lab:jw09",payload,"lab:jw09-idem")
    assert claim["claimed"] is True
    assert claim["witness"]["record"]["subject"]=="lab:jw09:lab:jw09-idem"
    assert restored.verify_dispatch_claim(
        "lab:jw09","lab:jw09-idem",payload,claim)
    with sqlite3.connect(witness_path) as db:
        after=db.execute(
            "SELECT record_json, signature FROM witness_checkpoints"
        ).fetchall()
    assert after==before
    assert restored.consume_and_claim(
        "lab:jw09",payload,"lab:jw09-idem")["execution_id"]==claim["execution_id"]


def test_jw10_pinned_witness_cannot_downgrade_with_same_key(tmp_path,monkeypatch):
    _keys(monkeypatch)
    db=str(tmp_path/"witness.sqlite")
    signed=IndependentWitness(db,verifiable_recovery=True)
    with pytest.raises(RuntimeError,match="downgrade prohibited"):
        IndependentWitness(db,verifiable_recovery=False)
    assert IndependentWitness(db,verifiable_recovery=True).key_id==signed.key_id


def test_jw10_pinned_witness_cannot_downgrade_by_removing_key(tmp_path,monkeypatch):
    _keys(monkeypatch)
    db=str(tmp_path/"witness.sqlite")
    signed=IndependentWitness(db,verifiable_recovery=True)
    monkeypatch.delenv("TENIR_WITNESS_PRIVATE_KEY_HEX",raising=False)
    with pytest.raises(RuntimeError,match="downgrade prohibited"):
        IndependentWitness(db)
    _keys(monkeypatch)
    assert IndependentWitness(db,verifiable_recovery=True).key_id==signed.key_id


def test_jw10_pinned_kernel_cannot_downgrade_with_keys_unchanged(tmp_path,monkeypatch):
    _keys(monkeypatch)
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    before=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    with pytest.raises(RuntimeError,match="kernel verifiable custody downgrade prohibited"):
        NativePositivePermitAuthority(backend,verifiable_recovery=False)
    assert NativePositivePermitAuthority(backend,verifiable_recovery=True).key_id==before.key_id


def test_jw10_pinned_kernel_cannot_downgrade_with_removed_keys(tmp_path,monkeypatch):
    _keys(monkeypatch)
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    NativePositivePermitAuthority(backend,verifiable_recovery=True)
    monkeypatch.delenv("TENIR_W2_PRIVATE_KEY_HEX",raising=False)
    monkeypatch.delenv("TENIR_WITNESS_PRIVATE_KEY_HEX",raising=False)
    with pytest.raises(RuntimeError,match="kernel verifiable custody downgrade prohibited"):
        NativePositivePermitAuthority(backend)
    _keys(monkeypatch)
    assert NativePositivePermitAuthority(backend,verifiable_recovery=True).verifiable_recovery


def test_jw10_legacy_witness_evidence_cannot_be_rebranded(tmp_path,monkeypatch):
    _keys(monkeypatch)
    db=str(tmp_path/"old-witness.sqlite")
    previous=IndependentWitness(db,verifiable_recovery=False)
    previous.issue("legacy-subject","ab"*32)
    with pytest.raises(RuntimeError,match="cannot upgrade legacy witness checkpoints"):
        IndependentWitness(db,verifiable_recovery=True)


def test_jw10_corrupted_kernel_pin_fails_even_with_original_key(tmp_path,monkeypatch):
    _keys(monkeypatch)
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    NativePositivePermitAuthority(backend,verifiable_recovery=True)
    with backend._connection() as db:
        db.execute("DELETE FROM w2_verifiable_custody")
    with pytest.raises(RuntimeError,match="custody pin missing"):
        NativePositivePermitAuthority(backend,verifiable_recovery=True)


def test_jw10_preopened_legacy_witness_is_fenced_after_other_instance_pins(
        tmp_path,monkeypatch):
    _keys(monkeypatch)
    store=str(tmp_path/"witness.sqlite")
    stale=IndependentWitness(store,verifiable_recovery=False)
    verified=IndependentWitness(store,verifiable_recovery=True)
    current=verified.issue("subject:verified","ab"*32)
    with pytest.raises(RuntimeError,match="old Witness instance fenced"):
        stale.issue("subject:legacy","ab"*32)
    assert stale.verify(current,subject="subject:verified") is False
    assert verified.verify(current,subject="subject:verified") is True


def test_jw10_preopened_legacy_kernel_writer_fenced_inside_transaction(
        tmp_path,monkeypatch):
    _keys(monkeypatch)
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    assert backend.register_resource("lab:budget",20)=="REGISTERED"
    stale=NativePositivePermitAuthority(backend,verifiable_recovery=False)
    NativePositivePermitAuthority(backend,verifiable_recovery=True)
    payload={
        "schema":"tenir-dispatch-command/v1",
        "resource_id":"lab:budget","amount":7,
        "action_type":"payment","agent_id":"lab-agent",
        "metadata":{"recipient":"lab"},
    }
    with pytest.raises(RuntimeError,match="previous Kernel instance fenced"):
        stale.admit_reserve_issue(
            "lab:downgrade-race","lab:eval",payload,resource_id="lab:budget")
    with backend._connection() as db:
        assert db.execute(
            "SELECT count(*) FROM w2_positive_v1_permits"
        ).fetchone()[0]==0
        assert db.execute(
            "SELECT count(*) FROM w2_positive_v1_outbox"
        ).fetchone()[0]==0
