"""Experimental G3 *fenced shadow* gates, with no execution cutover."""
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
from tenir_governance.operations.shadow_registry_migration import (
    ShadowMigrationHold, ShadowOperationMigrator,
)
from tenir_governance.operations.shadow_budget_parity import ShadowBudgetParityAudit
from tenir_governance.operations.shadow_g3_fence import G3ShadowFenceCoordinator


def _setup(tmp_path, *, state=OperationState.CREATED):
    source=tmp_path/"source-operations.json"
    registry=DurableOperationRegistry(str(source))
    assert registry.register(OperationRecord(
        idempotency_key="source-1",passage_id="passage-1",
        agent_id="agent-1",action_type="audit",state=state,
    ))
    base=tmp_path/"budget"
    backend=SQLiteConsensusBackend(str(base))
    assert backend.register_resource("budget",Decimal("100"))=="REGISTERED"
    migrator=ShadowOperationMigrator(registry,base/"consensus.sqlite3")
    migrator.verify_or_stage()
    audit=ShadowBudgetParityAudit(migrator)
    audit.verify()
    return registry,G3ShadowFenceCoordinator(audit),source,base/"consensus.sqlite3"


def _extra_record():
    return OperationRecord(
        idempotency_key="extra",passage_id="extra-p",agent_id="agent",
        action_type="audit",state=OperationState.CREATED)


def test_fence_is_idempotent_monotonic_and_never_grants_execution(tmp_path):
    registry,coordinator,source,db=_setup(tmp_path)
    original=source.read_bytes()
    stale=DurableOperationRegistry(str(source))  # opened BEFORE cutover ceremony
    first=coordinator.fence_shadow()
    assert first.status=="FENCED_SHADOW_HOLD"
    assert first.legacy_writes_fenced is True
    assert first.execution_authorized is False
    assert first.candidate_dispatch_authorized is False
    assert first.is_production_cutover is False
    assert coordinator.fence_shadow()==first
    assert coordinator.inspect()==first
    assert source.read_bytes()==original
    with pytest.raises(RuntimeError,match="G3 authority fence"):
        stale.register(_extra_record())
    assert registry.get("extra") is None
    with pytest.raises(RuntimeError,match="G3 authority fence"):
        registry.transition_to("source-1",OperationState.HOLD)
    assert registry.get("source-1").state==OperationState.CREATED


def test_crash_after_prepared_before_marker_does_not_prematurely_fence(tmp_path,monkeypatch):
    registry,coordinator,source,db=_setup(tmp_path)
    monkeypatch.setattr(coordinator,"_after_prepare",lambda: (_ for _ in ()).throw(
        RuntimeError("crash after prepare")))
    with pytest.raises(RuntimeError,match="crash after prepare"):
        coordinator.fence_shadow()
    assert not os.path.exists(coordinator.marker)
    assert not os.path.exists(coordinator.sentinel)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT phase FROM shadow_authority_fence").fetchone()[0]=="PREPARED"
    with pytest.raises(ShadowMigrationHold,match="not finalized"):
        coordinator.inspect()
    # Still a pre-fence JSON writer; changing source now invalidates epoch.
    assert registry.register(_extra_record())
    monkeypatch.setattr(coordinator,"_after_prepare",lambda:None)
    with pytest.raises(ShadowMigrationHold):
        coordinator.fence_shadow()


def test_crash_after_marker_blocks_writes_then_recovers_to_hold(tmp_path,monkeypatch):
    registry,coordinator,source,db=_setup(tmp_path)
    monkeypatch.setattr(coordinator,"_after_marker",lambda: (_ for _ in ()).throw(
        RuntimeError("crash after marker")))
    with pytest.raises(RuntimeError,match="crash after marker"):
        coordinator.fence_shadow()
    assert os.path.isfile(coordinator.marker)
    assert os.path.isfile(coordinator.sentinel)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT phase FROM shadow_authority_fence").fetchone()[0]=="PREPARED"
    with pytest.raises(RuntimeError,match="G3 authority fence"):
        DurableOperationRegistry(str(source)).register(_extra_record())
    with pytest.raises(ShadowMigrationHold,match="not finalized"):
        coordinator.inspect()
    restarted=G3ShadowFenceCoordinator(ShadowBudgetParityAudit(
        ShadowOperationMigrator(DurableOperationRegistry(str(source)),db)))
    assert restarted.fence_shadow().status=="FENCED_SHADOW_HOLD"
    assert restarted.inspect().execution_authorized is False


def test_deleted_fence_marker_does_not_reactivate_old_json_writers(tmp_path):
    registry,coordinator,source,_=_setup(tmp_path)
    coordinator.fence_shadow()
    os.unlink(coordinator.marker)
    with pytest.raises(RuntimeError,match="G3 authority fence"):
        DurableOperationRegistry(str(source)).register(_extra_record())
    with pytest.raises(ShadowMigrationHold,match="files incomplete"):
        coordinator.inspect()
    with pytest.raises(ShadowMigrationHold,match="manual recovery"):
        coordinator.fence_shadow()
    assert not os.path.exists(coordinator.marker)



def test_corrupted_fence_marker_holds_and_preserves_old_writer_block(tmp_path):
    registry,coordinator,source,_=_setup(tmp_path)
    coordinator.fence_shadow()
    with open(coordinator.marker,"w",encoding="utf-8") as fp:
        fp.write("{broken json")
    with pytest.raises(RuntimeError,match="G3 authority fence"):
        registry.register(_extra_record())
    with pytest.raises(ShadowMigrationHold,match="unreadable"):
        coordinator.inspect()
    with pytest.raises(ShadowMigrationHold,match="marker mismatch"):
        coordinator.fence_shadow()


def test_unsafe_non_drained_operation_refuses_fence(tmp_path):
    registry,coordinator,source,db=_setup(tmp_path,state=OperationState.RESERVED)
    with pytest.raises(ShadowMigrationHold,match="drain"):
        coordinator.fence_shadow()
    assert not os.path.exists(coordinator.marker)


def test_active_budget_reservation_prevents_G3_even_if_source_does_not_know(tmp_path):
    registry,coordinator,source,db=_setup(tmp_path)
    budget=SQLiteConsensusBackend(str(db.parent))
    assert budget.reserve("unknown-op",Decimal("5"),"budget").status=="RESERVED"
    with pytest.raises(ShadowMigrationHold):
        coordinator.fence_shadow()
    assert not os.path.exists(coordinator.marker)


def test_sqlite_fence_epoch_cannot_be_deleted_or_rewritten(tmp_path):
    registry,coordinator,source,db=_setup(tmp_path)
    coordinator.fence_shadow()
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.DatabaseError,match="cannot"):
            connection.execute("DELETE FROM shadow_authority_fence")
        with pytest.raises(sqlite3.DatabaseError,match="only advance"):
            connection.execute("UPDATE shadow_authority_fence SET phase='PREPARED'")
    assert coordinator.inspect().status=="FENCED_SHADOW_HOLD"


def test_changed_source_snapshot_before_g3_is_hold(tmp_path):
    registry,coordinator,source,db=_setup(tmp_path)
    assert registry.register(_extra_record())
    with pytest.raises(ShadowMigrationHold):
        coordinator.fence_shadow()
    assert not os.path.exists(coordinator.marker)


def _child_fence(source_path,db_path,start,results):
    try:
        r=DurableOperationRegistry(source_path)
        p=ShadowBudgetParityAudit(ShadowOperationMigrator(r,db_path))
        c=G3ShadowFenceCoordinator(p)
        if not start.wait(10):
            raise RuntimeError("no start signal")
        rep=c.fence_shadow()
        results.put(("ok",rep.status,rep.source_sha256))
    except BaseException as exc:
        results.put(("error",type(exc).__name__,str(exc)))


def test_two_processes_observe_one_monotonic_non_authorizing_epoch(tmp_path):
    registry,coordinator,source,db=_setup(tmp_path)
    ctx=multiprocessing.get_context("spawn")
    start,results=ctx.Event(),ctx.Queue()
    workers=[ctx.Process(target=_child_fence,args=(
        str(source),str(db),start,results)) for _ in range(2)]
    for worker in workers:worker.start()
    start.set()
    responses=[results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode==0
    assert all(r[0]=="ok" for r in responses),responses
    assert responses[0][1:]==responses[1][1:]
    assert coordinator.inspect().execution_authorized is False
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_authority_fence").fetchone()[0]==1


def _exit_at_checkpoint(source_path,db_path,point):
    r=DurableOperationRegistry(source_path)
    c=G3ShadowFenceCoordinator(ShadowBudgetParityAudit(
        ShadowOperationMigrator(r,db_path)))
    if point=="prepare":
        c._after_prepare=lambda:os._exit(86)
    else:
        c._after_marker=lambda:os._exit(87)
    c.fence_shadow()


@pytest.mark.parametrize("point,exitcode,should_be_fenced", [
    ("prepare",86,False),("marker",87,True),
])
def test_real_child_process_exit_and_resume(tmp_path,point,exitcode,should_be_fenced):
    registry,coordinator,source,db=_setup(tmp_path)
    ctx=multiprocessing.get_context("spawn")
    child=ctx.Process(target=_exit_at_checkpoint,args=(str(source),str(db),point))
    child.start()
    child.join(timeout=15)
    assert child.exitcode==exitcode
    assert os.path.isfile(coordinator.marker)==should_be_fenced
    if should_be_fenced:
        with pytest.raises(RuntimeError,match="G3 authority fence"):
            registry.register(_extra_record())
    recovered=G3ShadowFenceCoordinator(ShadowBudgetParityAudit(
        ShadowOperationMigrator(DurableOperationRegistry(str(source)),db)))
    assert recovered.fence_shadow().status=="FENCED_SHADOW_HOLD"
    assert recovered.inspect().execution_authorized is False


def test_removed_fence_sentinel_must_not_be_recreated_after_finalization(tmp_path):
    registry,coordinator,source,_=_setup(tmp_path)
    coordinator.fence_shadow()
    os.unlink(coordinator.sentinel)
    # Marker remains, so JSON writes stay blocked despite sentinel removal.
    with pytest.raises(RuntimeError,match="G3 authority fence"):
        registry.register(_extra_record())
    with pytest.raises(ShadowMigrationHold,match="manual recovery"):
        coordinator.fence_shadow()
    assert not os.path.exists(coordinator.sentinel)
