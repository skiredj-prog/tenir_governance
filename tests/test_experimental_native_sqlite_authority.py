"""P0 #67 slice 1: actual OperationRecord and budget in one SQLite commit.

This is a disposable laboratory lane, not a DualKernelGovernor adapter.
"""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from tenir_governance.operations.registry import OperationRecord, OperationState
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.experimental_native_sqlite_authority import (
    ExperimentalSqliteOperationAuthority, SqliteAuthorityHold,
)


def _record(key="lab:op-1", amount=20.0):
    return OperationRecord(
        idempotency_key=key, passage_id="pass-1",
        agent_id="alice", action_type="payment",
        amount=amount, resource_id="lab:budget",
    )


def _open(path):
    backend=SQLiteConsensusBackend(str(path))
    return backend,ExperimentalSqliteOperationAuthority(
        backend,experimental_only=True
    )


@pytest.fixture
def setup(tmp_path):
    path=tmp_path/"budget"
    backend,authority=_open(path)
    assert backend.register_resource("lab:budget",Decimal("100"))=="REGISTERED"
    return path,backend,authority


def test_staging_native_operation_and_reservation_is_atomic_and_hold(setup):
    path,backend,authority=setup
    issued=authority.admit_created(_record())
    assert issued.operation_state=="RESERVED"
    assert issued.budget_state=="RESERVED"
    assert issued.decision=="HOLD"
    assert issued.execution_authorized is False
    assert issued.is_production_authority is False
    state=backend.get_state("lab:budget")
    assert state.reserved==Decimal("20")
    assert state.committed==Decimal("0")
    assert len(state.reservations)==1
    assert state.reservations[0]["reservation_id"]==issued.reservation_id
    assert authority.inspect(_record())==issued


def test_idempotent_replay_restart_does_not_reserve_again(setup):
    path,backend,authority=setup
    first=authority.admit_created(_record())
    second_backend,second=_open(path)
    assert second.admit_created(_record())==first
    assert second.inspect(_record())==first
    assert second_backend.get_state("lab:budget").reserved==Decimal("20")


@pytest.mark.parametrize("mutation",[
    {"amount":21.0},
    {"passage_id":"other"},
    {"payload_hash":"cd"*32},
    {"institutional_scope":"other"},
])
def test_conflicting_native_identity_is_not_replayed(setup,mutation):
    _path,backend,authority=setup
    authority.admit_created(_record())
    altered=replace(_record(),**mutation)
    with pytest.raises(SqliteAuthorityHold,match="replay or state mismatch"):
        authority.admit_created(altered)
    assert backend.get_state("lab:budget").reserved==Decimal("20")


def test_missing_capacity_cannot_register_operation(setup):
    path,backend,authority=setup
    with pytest.raises(SqliteAuthorityHold,match="INSUFFICIENT_BUDGET"):
        authority.admit_created(_record(amount=101.0))
    with sqlite3.connect(backend._database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM experimental_native_operations").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]==0
    assert backend.get_state("lab:budget").reserved==Decimal("0")


def test_orphan_reservation_from_legacy_budget_fails_closed(setup):
    _path,backend,authority=setup
    assert backend.reserve("lab:op-1",Decimal("20"),"lab:budget").status=="RESERVED"
    with pytest.raises(SqliteAuthorityHold,match="partially"):
        authority.admit_created(_record())
    assert backend.get_state("lab:budget").reserved==Decimal("20")


def test_unsafe_mutable_input_or_non_lab_identity_fails_closed(setup):
    _path,backend,authority=setup
    for record in (
        replace(_record(),state=OperationState.DISPATCHING),
        replace(_record(),idempotency_key="production-id"),
        replace(_record(),resource_id="budget"),
        replace(_record(),budget_reserved=True),
        replace(_record(),payload_hash="invalid"),
    ):
        with pytest.raises(SqliteAuthorityHold):
            authority.admit_created(record)
    assert backend.get_state("lab:budget").reserved==Decimal("0")


def test_explicit_opt_in_required_and_default_governor_unmodified(tmp_path):
    backend=SQLiteConsensusBackend(str(tmp_path/"budget"))
    with pytest.raises(SqliteAuthorityHold,match="experimental_only"):
        ExperimentalSqliteOperationAuthority(backend)
    assert not hasattr(backend,"operation_authority")


def test_injected_before_commit_rolls_back_operation_and_reservation(setup,monkeypatch):
    path,backend,authority=setup
    def fail(label):
        if label=="experimental_native_admit":
            raise RuntimeError("simulated crash")
    monkeypatch.setattr(backend,"_before_commit",fail)
    with pytest.raises(RuntimeError,match="simulated"):
        authority.admit_created(_record())
    restored,other=_open(path)
    assert restored.get_state("lab:budget").reserved==Decimal("0")
    with sqlite3.connect(restored._database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM experimental_native_operations").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]==0


def _race(path,key,amount,start,result):
    try:
        _backend,authority=_open(path)
        if not start.wait(timeout=10):
            raise RuntimeError("no start signal")
        entry=authority.admit_created(_record(key,amount))
        result.put(("ok",entry.reservation_id))
    except BaseException as exc:
        result.put(("hold",type(exc).__name__,str(exc)))


def test_cross_process_budget_limit_only_one_winner(setup):
    path,backend,authority=setup
    authority.admit_created(_record(amount=90.0))
    ctx=multiprocessing.get_context("spawn")
    start,results=ctx.Event(),ctx.Queue()
    children=[ctx.Process(target=_race,args=(
        str(path),"lab:race-"+str(i),7.0,start,results
    )) for i in range(2)]
    for p in children:p.start()
    start.set()
    responses=[results.get(timeout=30) for _ in children]
    for p in children:
        p.join(timeout=15)
        assert p.exitcode==0
    assert sum(r[0]=="ok" for r in responses)==1,responses
    assert sum(r[0]=="hold" for r in responses)==1,responses
    assert backend.get_state("lab:budget").reserved==Decimal("97")


def _exit_before_commit(path):
    backend,authority=_open(path)
    backend._before_commit=lambda label: os._exit(86)
    authority.admit_created(_record())


def test_hard_exit_before_commit_has_no_half_written_operation(setup):
    path,backend,authority=setup
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_exit_before_commit,args=(str(path),))
    child.start()
    child.join(timeout=15)
    assert child.exitcode==86
    reloaded,new_authority=_open(path)
    assert reloaded.get_state("lab:budget").reserved==Decimal("0")
    with pytest.raises(SqliteAuthorityHold,match="partially"):
        new_authority.inspect(_record())


def test_corrupted_record_or_attempted_mutation_cannot_grant_dispatch(setup):
    _path,backend,authority=setup
    authority.admit_created(_record())
    with sqlite3.connect(backend._database_path) as db:
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            db.execute("UPDATE experimental_native_operations SET state='DISPATCHING'")
        with pytest.raises(sqlite3.DatabaseError,match="cannot"):
            db.execute("DELETE FROM experimental_native_operations")
    assert authority.inspect(_record()).execution_authorized is False


def test_replay_refuses_drifts_in_accounting_counter(setup):
    _path,backend,authority=setup
    authority.admit_created(_record())
    with sqlite3.connect(backend._database_path) as db:
        db.execute("UPDATE resources SET reserved='21' WHERE resource_id='lab:budget'")
    with pytest.raises(SqliteAuthorityHold,match="I8 counter"):
        authority.admit_created(_record())


def test_never_use_backend_containing_real_budget_or_reservations(setup):
    _path,backend,authority=setup
    assert backend.register_resource("production-budget",Decimal("100"))=="REGISTERED"
    with pytest.raises(SqliteAuthorityHold,match="non-laboratory"):
        authority.admit_created(_record())


def test_existing_g3_epoch_refuses_candidate_admission(setup):
    _path,backend,authority=setup
    with sqlite3.connect(backend._database_path) as db:
        db.execute("CREATE TABLE shadow_authority_fence(epoch INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO shadow_authority_fence VALUES (1)")
    with pytest.raises(SqliteAuthorityHold,match="G3 fenced shadow"):
        authority.admit_created(_record())
    assert backend.get_state("lab:budget").reserved==Decimal("0")
