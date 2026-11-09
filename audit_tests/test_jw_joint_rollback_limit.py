"""Scope-falsification test: two SQLite stores and local pins can roll back together.

This is an EXPECTED GAP, not a successful anti-rollback assertion. The test
shows WHY the global JW-01..10 VERIFIED claim must remain withheld.
"""
from __future__ import annotations

import sqlite3

import pytest

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_integration.independent_witness import IndependentWitness


def _snapshot(src, dst):
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)


def test_joint_kernel_and_witness_snapshot_restore_is_not_detected_locally(
        tmp_path,monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32,64)).hex())
    kernel_directory=tmp_path/"kernel"
    backend=SQLiteConsensusBackend(str(kernel_directory))
    assert backend.register_resource("lab:budget",20)=="REGISTERED"
    native=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    payload={
        "schema":"tenir-dispatch-command/v1",
        "resource_id":"lab:budget","amount":7,"action_type":"payment",
        "agent_id":"lab-agent","metadata":{"recipient":"lab-no-effect"},
    }
    native.admit_reserve_issue(
        "lab:rollback","lab:evaluation",payload,resource_id="lab:budget",
    )
    kernel=backend._database_path
    witness=native.witness.db_path
    before_kernel=tmp_path/"prior_kernel.sqlite3"
    before_witness=tmp_path/"prior_witness.sqlite3"
    _snapshot(kernel,before_kernel)
    _snapshot(witness,before_witness)

    claim=native.consume_and_claim("lab:rollback",payload,"lab:rollback-idem")
    assert claim["claimed"] and claim["witness"]["record"]["revision"]==1
    assert native.verify_dispatch_claim(
        "lab:rollback","lab:rollback-idem",payload,claim)
    with sqlite3.connect(witness) as db:
        assert db.execute("SELECT COUNT(*) FROM witness_checkpoints").fetchone()[0]==1

    # An attacker with power to restore both DBs (without changing the
    # injected keys) can restore an internally consistent old snapshot.
    _snapshot(before_kernel,kernel)
    _snapshot(before_witness,witness)
    restarted=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    assert restarted.key_id==native.key_id
    assert restarted.witness.key_id==native.witness.key_id
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_outbox").fetchone()[0]==0
    with sqlite3.connect(witness) as db:
        assert db.execute("SELECT COUNT(*) FROM witness_checkpoints").fetchone()[0]==0

    # The old proof is no longer considered present; both local stores
    # fail to report that *newer* state once existed. This demonstrates
    # missing monotonic EXTERNAL anchor, not provider double execution.
    assert not restarted.witness.verify(
        claim["witness"],subject="lab:rollback:lab:rollback-idem")
    assert not restarted.verify_dispatch_claim(
        "lab:rollback","lab:rollback-idem",payload,claim)
    replay=restarted.consume_and_claim(
        "lab:rollback",payload,"lab:rollback-idem")
    assert replay["claimed"] and not replay.get("replayed", False)
    assert replay["execution_id"]!=claim["execution_id"]
    assert replay["claim_token"]!=claim["claim_token"]
    # No provider execution is attempted in this test.


def test_attached_anchor_gate_now_refuses_the_previously_undetected_rollback(
        tmp_path,monkeypatch):
    """Companion to the falsification above: attaching the external cold-restart
    gate makes the joint rollback refusable on the native admission path — and,
    as the mandatory negative control, a stale reader replaying a validly signed
    old checkpoint still defeats it (signature is not freshness).
    """
    from copy import deepcopy
    from w2_integration.external_cold_restart_anchor import (
        ExternalColdRestartAnchorGate, ExternalAnchorHold,
    )
    from w2_integration.cold_restart_anchor_publisher import (
        MonotoneAnchorAuthority, publish_cold_restart_anchor,
    )

    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32,64)).hex())
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    assert backend.register_resource("lab:budget",20)=="REGISTERED"
    native=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    payload={"schema":"tenir-dispatch-command/v1","resource_id":"lab:budget",
             "amount":7,"action_type":"payment","agent_id":"lab-agent",
             "metadata":{"recipient":"lab-no-effect"}}
    subject="lab:joint"
    publisher=MonotoneAnchorAuthority()
    pre_kernel=tmp_path/"pre_kernel.sqlite3"; pre_witness=tmp_path/"pre_witness.sqlite3"
    _snapshot(backend._database_path,pre_kernel)
    _snapshot(native.witness.db_path,pre_witness)
    stale=publish_cold_restart_anchor(native,publisher,subject=subject,generation=1).envelope
    native.admit_reserve_issue("lab:rollback","lab:eval",payload,resource_id="lab:budget")
    native.consume_and_claim("lab:rollback",payload,"lab:rollback-idem")
    publish_cold_restart_anchor(native,publisher,subject=subject,generation=2)

    _snapshot(pre_kernel,backend._database_path)
    _snapshot(pre_witness,native.witness.db_path)
    restarted=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    live_gate=ExternalColdRestartAnchorGate(
        reader=publisher,trusted_public_key=publisher.public_key_bytes,
        required_topology=native.topology_id)
    restarted.attach_cold_restart_anchor_gate(live_gate,subject=subject)
    with pytest.raises(ExternalAnchorHold,match="rollback or drift"):
        restarted.admit_reserve_issue("lab:after","lab:eval",payload,resource_id="lab:budget")

    class _Replay:
        def read_current(self,_): return deepcopy(stale)
    stale_gate=ExternalColdRestartAnchorGate(
        reader=_Replay(),trusted_public_key=publisher.public_key_bytes,
        required_topology=native.topology_id)
    restarted.attach_cold_restart_anchor_gate(stale_gate,subject=subject)
    # KNOWN GAP: the stale-but-valid checkpoint is accepted; rollback undetected.
    accepted=restarted.admit_reserve_issue(
        "lab:after","lab:eval",payload,resource_id="lab:budget")
    assert accepted["permit_id"]=="permit-lab:after"


def test_witness_only_rollback_is_detected_by_preserved_kernel_claim(
        tmp_path,monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX",bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX",bytes(range(32,64)).hex())
    backend=SQLiteConsensusBackend(str(tmp_path/"kernel"))
    assert backend.register_resource("lab:budget",20)=="REGISTERED"
    authority=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    payload={"schema":"tenir-dispatch-command/v1",
             "resource_id":"lab:budget","amount":7,
             "action_type":"payment","agent_id":"lab-agent",
             "metadata":{"recipient":"lab-no-effect"}}
    authority.admit_reserve_issue(
        "lab:witness-only","lab:evaluation",payload,resource_id="lab:budget")
    witness=authority.witness.db_path
    old=tmp_path/"prior_witness.sqlite3"
    _snapshot(witness,old)
    claim=authority.consume_and_claim(
        "lab:witness-only",payload,"lab:witness-only-idem")
    assert authority.verify_dispatch_claim(
        "lab:witness-only","lab:witness-only-idem",payload,claim)
    _snapshot(old,witness)
    restarted=NativePositivePermitAuthority(backend,verifiable_recovery=True)
    assert not restarted.verify_dispatch_claim(
        "lab:witness-only","lab:witness-only-idem",payload,claim)
