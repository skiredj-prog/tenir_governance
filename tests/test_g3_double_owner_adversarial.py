"""P0 BLOCKER G3: adversarial JSON and SQLite same-op double owner race.

These tests must never be interpreted as a production cutover guarantee:
the gate is single-host, cooperative, lab-only and denies provider dispatch.
Unpatched legacy binaries, direct SQLite entry points and remote writers
still prohibit an institutional cutover.
"""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
from decimal import Decimal

import pytest

from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.experimental_g3_exclusive_owner import (
    G3ExperimentalExclusiveOwner,
)
from tenir_governance.operations.experimental_native_sqlite_authority import (
    ExperimentalSqliteOperationAuthority, SqliteAuthorityHold,
)


def _record():
    return OperationRecord(
        idempotency_key="lab:op-1",passage_id="lab:passage-1",
        agent_id="lab:agent-1",action_type="payment",
        resource_id="lab:budget",amount=20.0,
    )


def _open(json_path,budget_path):
    registry=DurableOperationRegistry(str(json_path))
    backend=SQLiteConsensusBackend(str(budget_path))
    gate=G3ExperimentalExclusiveOwner(registry,backend,experimental_only=True)
    return registry,backend,gate


@pytest.fixture
def scope(tmp_path):
    json_path=tmp_path/"legacy.json"
    budget_path=tmp_path/"sqlite-budget"
    registry=DurableOperationRegistry(str(json_path))
    assert registry.register(_record())
    backend=SQLiteConsensusBackend(str(budget_path))
    assert backend.register_resource("lab:budget",Decimal("100"))=="REGISTERED"
    _,_,gate=_open(json_path,budget_path)
    return json_path,budget_path,gate


def _snapshot(json_path,budget_path):
    registry=DurableOperationRegistry(str(json_path))
    budget=SQLiteConsensusBackend(str(budget_path)).get_state("lab:budget")
    return registry.get("lab:op-1").state,budget.reserved,budget.committed


def test_json_owner_transition_first_prevents_sqlite_takeover(scope):
    json_path,budget_path,gate=scope
    json_owner=DurableOperationRegistry(str(json_path))
    ok,_=json_owner.transition_to("lab:op-1",OperationState.RESERVED)
    assert ok
    with pytest.raises(SqliteAuthorityHold,match="already transitioned"):
        gate.activate()
    with pytest.raises(SqliteAuthorityHold,match="already transitioned|not active|epoch absent"):
        gate.candidate_transition(_record())
    state,reserved,committed=_snapshot(json_path,budget_path)
    assert state is OperationState.RESERVED
    assert reserved==Decimal("0") and committed==Decimal("0")


def test_sqlite_fence_first_blocks_preopened_json_writer_and_allows_only_lab_sqlite(scope):
    json_path,budget_path,gate=scope
    old_registry=DurableOperationRegistry(str(json_path))
    fenced=gate.activate()
    assert fenced.state=="SQLITE_LAB_OWNER_HOLD"
    assert fenced.json_writes_fenced
    assert fenced.execution_authorized is False
    assert fenced.production_cutover is False
    with pytest.raises(RuntimeError,match="G3_SQLITE_OWNER_HOLD"):
        old_registry.transition_to("lab:op-1",OperationState.RESERVED)
    with pytest.raises(RuntimeError,match="G3_SQLITE_OWNER_HOLD"):
        old_registry.register(OperationRecord(
            idempotency_key="lab:other",passage_id="lab:p",
            agent_id="lab:agent",action_type="audit",
        ))
    sqlite_receipt=gate.candidate_transition(_record())
    assert sqlite_receipt.operation_state=="RESERVED"
    assert sqlite_receipt.decision=="HOLD"
    assert sqlite_receipt.execution_authorized is False
    assert sqlite_receipt.is_production_authority is False
    assert gate.candidate_transition(_record())==sqlite_receipt
    state,reserved,committed=_snapshot(json_path,budget_path)
    assert state is OperationState.CREATED
    assert reserved==Decimal("20") and committed==Decimal("0")


def _json_claim(json_path,start,results):
    try:
        registry=DurableOperationRegistry(str(json_path))
        assert start.wait(timeout=10)
        ok,_=registry.transition_to("lab:op-1",OperationState.RESERVED)
        results.put(("JSON","AUTHORIZED" if ok else "HOLD"))
    except BaseException as exc:
        results.put(("JSON","HOLD",type(exc).__name__,str(exc)))


def _sqlite_claim(json_path,budget_path,start,results):
    try:
        _registry,_backend,gate=_open(json_path,budget_path)
        assert start.wait(timeout=10)
        gate.activate()
        answer=gate.candidate_transition(_record())
        results.put(("SQLITE","AUTHORIZED" if answer.operation_state=="RESERVED" else "HOLD"))
    except BaseException as exc:
        results.put(("SQLITE","HOLD",type(exc).__name__,str(exc)))


@pytest.mark.parametrize("iteration",range(4))
def test_real_two_process_double_owner_race_never_both_authorize(tmp_path,iteration):
    parent=tmp_path/f"race-{iteration}"
    parent.mkdir()
    json_path=parent/"legacy.json"
    budget_path=parent/"budget"
    registry=DurableOperationRegistry(str(json_path))
    assert registry.register(_record())
    backend=SQLiteConsensusBackend(str(budget_path))
    assert backend.register_resource("lab:budget",Decimal("100"))=="REGISTERED"
    ctx=multiprocessing.get_context("spawn")
    start,results=ctx.Event(),ctx.Queue()
    processes=[
        ctx.Process(target=_json_claim,args=(str(json_path),start,results)),
        ctx.Process(target=_sqlite_claim,args=(
            str(json_path),str(budget_path),start,results)),
    ]
    for proc in processes:proc.start()
    start.set()
    answers=[results.get(timeout=35) for _ in processes]
    for proc in processes:
        proc.join(timeout=15)
        assert proc.exitcode==0
    owners={x[0]:x[1] for x in answers}
    assert sorted(owners)==["JSON","SQLITE"],answers
    assert sum(v=="AUTHORIZED" for v in owners.values())==1,answers
    state,reserved,committed=_snapshot(json_path,budget_path)
    assert committed==Decimal("0")
    assert (state,reserved) in (
        (OperationState.RESERVED,Decimal("0")),
        (OperationState.CREATED,Decimal("20")),
    )


def _crash_at_boundary(json_path,budget_path,stage):
    _,_,gate=_open(json_path,budget_path)
    if stage=="prepared":
        gate._before_sentinel=lambda:os._exit(86)
    else:
        gate._after_sentinel=lambda:os._exit(87)
    gate.activate()


@pytest.mark.parametrize("stage,exitcode",[
    ("prepared",86),("sentinel",87),
])
def test_crash_boundary_never_opens_two_owners(scope,stage,exitcode):
    json_path,budget_path,gate=scope
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_crash_at_boundary,
                      args=(str(json_path),str(budget_path),stage))
    child.start()
    child.join(timeout=20)
    assert child.exitcode==exitcode
    legacy=DurableOperationRegistry(str(json_path))
    if stage=="prepared":
        # PREPARED is not an active owner; old JSON may still transition.
        with pytest.raises(SqliteAuthorityHold,match="not active|epoch absent"):
            gate.candidate_transition(_record())
        assert legacy.transition_to("lab:op-1",OperationState.RESERVED)[0] is True
        with pytest.raises(SqliteAuthorityHold,match="changed|transitioned"):
            gate.activate()
    else:
        # Even with ACTIVE absent the durable sentinel already blocks JSON.
        with pytest.raises(RuntimeError,match="G3_SQLITE_OWNER_HOLD"):
            legacy.transition_to("lab:op-1",OperationState.RESERVED)
        with pytest.raises(SqliteAuthorityHold,match="not active"):
            gate.candidate_transition(_record())
        assert gate.activate().execution_authorized is False
        assert gate.candidate_transition(_record()).execution_authorized is False


def test_missing_finalized_marker_blocks_json_and_sqlite(scope):
    json_path,budget_path,gate=scope
    gate.activate()
    os.unlink(gate.marker)
    with pytest.raises(RuntimeError,match="G3_SQLITE_OWNER_HOLD"):
        DurableOperationRegistry(str(json_path)).transition_to(
            "lab:op-1",OperationState.RESERVED)
    with pytest.raises(SqliteAuthorityHold,match="fence missing"):
        gate.candidate_transition(_record())
    with pytest.raises(SqliteAuthorityHold,match="manual recovery"):
        gate.activate()


def test_g3_strict_opt_in_and_no_direct_provider_dispatch(scope):
    json_path,budget_path,gate=scope
    with pytest.raises(SqliteAuthorityHold,match="explicit experimental"):
        G3ExperimentalExclusiveOwner(
            DurableOperationRegistry(str(json_path)),
            SQLiteConsensusBackend(str(budget_path)),
        )
    assert not hasattr(gate,"execute")
    assert not hasattr(gate,"dispatch")
    assert not hasattr(gate,"commit")
    assert not hasattr(gate,"authorize_retry")


def test_existing_json_dispatch_claim_forbids_late_sqlite_owner(scope):
    json_path,budget_path,gate=scope
    old=DurableOperationRegistry(str(json_path))
    assert old.transition_to("lab:op-1",OperationState.RESERVED)[0]
    assert old.transition_to("lab:op-1",OperationState.DISPATCHING,claimed_by="old-worker")[0]
    with pytest.raises(SqliteAuthorityHold,match="already transitioned"):
        gate.activate()
    with pytest.raises(SqliteAuthorityHold):
        gate.candidate_transition(_record())
    state,reserved,committed=_snapshot(json_path,budget_path)
    assert state is OperationState.DISPATCHING
    assert reserved==Decimal("0") and committed==Decimal("0")


def test_fenced_json_existing_instance_cannot_claim_dispatch_or_budget_flags(scope):
    json_path,budget_path,gate=scope
    old=DurableOperationRegistry(str(json_path))
    gate.activate()
    with pytest.raises(RuntimeError,match="G3_SQLITE_OWNER_HOLD"):
        old.set_budget_flags("lab:op-1",budget_reserved=True,reservation_id="rogue")
    assert old.get("lab:op-1").budget_reserved is False
    with pytest.raises(RuntimeError,match="G3_SQLITE_OWNER_HOLD"):
        old.transition_to("lab:op-1",OperationState.RESERVED)
    assert gate.candidate_transition(_record()).execution_authorized is False


def test_negative_control_direct_sqlite_no_longer_creates_second_reservation(scope):
    """Previously failing negative control: direct API MUST now HOLD."""
    json_path,budget_path,gate=scope
    legacy=DurableOperationRegistry(str(json_path))
    assert legacy.transition_to("lab:op-1",OperationState.RESERVED)[0]
    direct=ExperimentalSqliteOperationAuthority(
        SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
    with pytest.raises(SqliteAuthorityHold,match="epoch absent|not active"):
        direct.admit_created(_record())
    # Even raw backend.reserve cannot insert: the DB trigger enforces
    # native operation/active-epoch binding on reservation insertion.
    with pytest.raises(sqlite3.DatabaseError,match="G3 epoch-bound"):
        SQLiteConsensusBackend(str(budget_path)).reserve(
            "lab:op-1",Decimal("20"),"lab:budget")
    assert legacy.get("lab:op-1").state is OperationState.RESERVED
    assert SQLiteConsensusBackend(str(budget_path)).get_state(
        "lab:budget").reserved==Decimal("0")
    with pytest.raises(SqliteAuthorityHold,match="already transitioned"):
        gate.activate()


def test_direct_sqlite_after_valid_g3_epoch_uses_same_guards(scope):
    json_path,budget_path,gate=scope
    epoch=gate.activate()
    direct=ExperimentalSqliteOperationAuthority(
        SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
    admitted=direct.admit_created(_record())
    assert admitted.authority_epoch==epoch.epoch
    assert admitted.execution_authorized is False
    assert gate.candidate_transition(_record())==admitted
    with sqlite3.connect(SQLiteConsensusBackend(
            str(budget_path))._database_path) as db:
        stored=db.execute(
            "SELECT authority_epoch FROM experimental_native_operations "
            "WHERE operation_key='lab:op-1'").fetchone()
        assert stored==(epoch.epoch,)


def test_epoch_or_marker_loss_blocks_direct_replay(scope):
    json_path,budget_path,gate=scope
    gate.activate()
    direct=ExperimentalSqliteOperationAuthority(
        SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
    direct.admit_created(_record())
    os.unlink(gate.marker)
    with pytest.raises(SqliteAuthorityHold,match="fence missing"):
        direct.admit_created(_record())


def test_source_changed_while_fenced_blocks_direct_sqlite(scope):
    json_path,budget_path,gate=scope
    gate.activate()
    direct=ExperimentalSqliteOperationAuthority(
        SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
    # Simulates a privileged or unpatched JSON writer bypassing its sentinel.
    with open(json_path,"r+",encoding="utf-8") as fp:
        import json
        snapshot=json.load(fp)
        snapshot["lab:op-1"]["action_type"]="altered"
        fp.seek(0)
        json.dump(snapshot,fp)
        fp.truncate()
    with pytest.raises(SqliteAuthorityHold,match="source changed"):
        direct.admit_created(_record())
    assert SQLiteConsensusBackend(str(budget_path)).get_state(
        "lab:budget").reserved==Decimal("0")


def _sqlite_direct_claim(budget_path,start,results):
    try:
        direct=ExperimentalSqliteOperationAuthority(
            SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
        assert start.wait(timeout=10)
        receipt=direct.admit_created(_record())
        results.put(("DIRECT_SQLITE","AUTHORIZED",receipt.authority_epoch))
    except BaseException as exc:
        results.put(("DIRECT_SQLITE","HOLD",type(exc).__name__,str(exc)))


@pytest.mark.parametrize("fence_first", [False,True])
@pytest.mark.parametrize("round_index",range(3))
def test_json_vs_unrouted_sqlite_two_process_race_cannot_double_own(
        tmp_path,fence_first,round_index):
    """Independent public entrypoints, not via G3 wrapper."""
    base=tmp_path/("fenced" if fence_first else "unfenced")/str(round_index)
    base.mkdir(parents=True)
    source=base/"registry.json"
    budget=base/"budget"
    old=DurableOperationRegistry(str(source))
    assert old.register(_record())
    backend=SQLiteConsensusBackend(str(budget))
    assert backend.register_resource("lab:budget",Decimal("100"))=="REGISTERED"
    coordinator=G3ExperimentalExclusiveOwner(
        old,backend,experimental_only=True)
    if fence_first:
        coordinator.activate()
    ctx=multiprocessing.get_context("spawn")
    start,results=ctx.Event(),ctx.Queue()
    workers=[
        ctx.Process(target=_json_claim,args=(str(source),start,results)),
        ctx.Process(target=_sqlite_direct_claim,args=(str(budget),start,results)),
    ]
    for child in workers: child.start()
    start.set()
    outcomes=[results.get(timeout=35) for _ in workers]
    for child in workers:
        child.join(timeout=15)
        assert child.exitcode==0
    results_by_owner={x[0]:x[1] for x in outcomes}
    assert sum(v=="AUTHORIZED" for v in results_by_owner.values())==1,outcomes
    state,reserved,committed=_snapshot(source,budget)
    assert committed==Decimal("0")
    if fence_first:
        assert state is OperationState.CREATED
        assert reserved==Decimal("20")
    else:
        assert state is OperationState.RESERVED
        assert reserved==Decimal("0")


def test_epoch_change_denies_direct_sqlite_replay(scope):
    json_path,budget_path,gate=scope
    gate.activate()
    direct=ExperimentalSqliteOperationAuthority(
        SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
    first=direct.admit_created(_record())
    dbpath=SQLiteConsensusBackend(str(budget_path))._database_path
    # Authorized G3 epochs are immutable; deliberately bypass SQL trigger as
    # a privileged attacker to assert the recheck is nevertheless fail-closed.
    with sqlite3.connect(dbpath) as db:
        db.execute("DROP TRIGGER g3_exclusive_monotonic")
        db.execute("UPDATE experimental_g3_exclusive_epoch SET epoch=?",
                   ("tampered-epoch",))
    with pytest.raises(SqliteAuthorityHold,match="marker/epoch mismatch"):
        direct.admit_created(_record())
    assert SQLiteConsensusBackend(str(budget_path)).get_state(
        "lab:budget").reserved==Decimal("20")
    assert first.authority_epoch!="tampered-epoch"


def test_direct_backend_commit_release_and_delete_blocked_until_g4(scope):
    json_path,budget_path,gate=scope
    gate.activate()
    direct=ExperimentalSqliteOperationAuthority(
        SQLiteConsensusBackend(str(budget_path)),experimental_only=True)
    receipt=direct.admit_created(_record())
    backend=SQLiteConsensusBackend(str(budget_path))
    for method in (backend.commit,backend.release):
        with pytest.raises(sqlite3.DatabaseError,match="G3 budget settlement unavailable"):
            method(receipt.reservation_id)
    with sqlite3.connect(backend._database_path) as db:
        with pytest.raises(sqlite3.DatabaseError,match="G3 budget deletion unavailable"):
            db.execute("DELETE FROM reservations WHERE reservation_id=?",
                       (receipt.reservation_id,))
    after=backend.get_state("lab:budget")
    assert after.reserved==Decimal("20")
    assert after.committed==Decimal("0")
    assert direct.admit_created(_record())==receipt
