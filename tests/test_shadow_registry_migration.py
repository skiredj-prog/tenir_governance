"""G0/G2 conservative shadow migration of production JSON registry to SQLite.

No test claims cutover authority. Real source JSON is never modified by
the migrator; a source divergence always requires a new reviewed baseline.
"""
import hashlib
import json
import multiprocessing
import sqlite3

import pytest

from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState,
)
from tenir_governance.operations.shadow_registry_migration import (
    ShadowOperationMigrator, ShadowMigrationHold,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def _setup(tmp_path, db_path=None):
    source = tmp_path/"operations.json"
    registry = DurableOperationRegistry(str(source))
    assert registry.register(OperationRecord(
        idempotency_key="op-1", passage_id="passage-1",
        agent_id="agent-1",action_type="payment", amount=10.0,
        resource_id="budget",state=OperationState.UNKNOWN))
    db = db_path or (tmp_path/"candidate.sqlite")
    migrator = ShadowOperationMigrator(registry,db)
    return registry,migrator,source,db


def _counts(path):
    with sqlite3.connect(path) as db:
        a=db.execute("SELECT COUNT(*) FROM shadow_migration_operations").fetchone()[0]
        b=db.execute("SELECT COUNT(*) FROM shadow_migration_epoch").fetchone()[0]
    return a,b


def test_shadow_import_is_exact_repeatable_and_never_authorizes_execution(tmp_path):
    registry,migrator,source,db=_setup(tmp_path)
    before=source.read_bytes()
    report=migrator.verify_or_stage()
    assert report.status=="SHADOW_MATCHED_NO_CUTOVER"
    assert report.records==1
    assert report.execution_authorized is False
    assert report.authoritative_source=="JSON_REGISTRY"
    assert report==migrator.verify_or_stage()
    assert _counts(db)==(1,1)
    assert source.read_bytes()==before
    assert registry.get("op-1").state==OperationState.UNKNOWN


def test_shadow_restart_reuses_frozen_digest(tmp_path):
    registry,migrator,source,db=_setup(tmp_path)
    first=migrator.verify_or_stage()
    reread=ShadowOperationMigrator(DurableOperationRegistry(str(source)),db)
    assert reread.inspect()==first


def test_actual_registry_transition_invalidates_shadow(tmp_path):
    registry,migrator,_,db=_setup(tmp_path)
    migrator.verify_or_stage()
    ok,_=registry.transition_to("op-1",OperationState.FAILED)
    assert ok
    with pytest.raises(ShadowMigrationHold,match="source changed"):
        migrator.verify_or_stage()
    assert _counts(db)==(1,1)


def test_new_source_operation_is_not_silently_added(tmp_path):
    registry,migrator,_,db=_setup(tmp_path)
    migrator.verify_or_stage()
    assert registry.register(OperationRecord(
        idempotency_key="op-2",passage_id="passage-2",agent_id="agent-2",
        action_type="audit",state=OperationState.CREATED))
    with pytest.raises(ShadowMigrationHold,match="source changed"):
        migrator.verify_or_stage()
    assert _counts(db)==(1,1)


def test_quarantine_blocks_shadow(tmp_path):
    registry,migrator,_,db=_setup(tmp_path)
    registry.set_quarantine(True,locked_by="security",incident_id="incident-1")
    with pytest.raises(ShadowMigrationHold,match="quarantine"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)


def test_orphan_duplicate_and_malformed_source_fail_closed(tmp_path):
    registry,migrator,source,db=_setup(tmp_path)
    source.write_text('{"op-1":{"idempotency_key":"different"}}',encoding="utf-8")
    with pytest.raises(ShadowMigrationHold,match="mismatch"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)


def test_unknown_record_field_rejected_without_partial_copy(tmp_path):
    _,migrator,source,db=_setup(tmp_path)
    payload=json.loads(source.read_text())
    payload["op-1"]["unreviewed_extra"]={"allow":True}
    source.write_text(json.dumps(payload))
    with pytest.raises(ShadowMigrationHold,match="unknown"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)


def test_snapshot_transaction_rollback_on_fault(tmp_path,monkeypatch):
    _,migrator,_,db=_setup(tmp_path)
    def fail():
        raise RuntimeError("injected before COMMIT")
    monkeypatch.setattr(migrator,"_before_commit",fail)
    with pytest.raises(RuntimeError,match="injected"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)


def test_source_missing_after_import_is_hold(tmp_path):
    _,migrator,source,db=_setup(tmp_path)
    migrator.verify_or_stage()
    source.unlink()
    with pytest.raises(ShadowMigrationHold,match="source file missing"):
        migrator.inspect()
    assert _counts(db)==(1,1)


def test_modified_staged_row_fails_closed(tmp_path):
    _,migrator,_,db=_setup(tmp_path)
    migrator.verify_or_stage()
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            connection.execute("UPDATE shadow_migration_operations SET record_sha256='evil'")
        with pytest.raises(sqlite3.DatabaseError,match="cannot"):
            connection.execute("DELETE FROM shadow_migration_epoch")
    assert migrator.inspect().status=="SHADOW_MATCHED_NO_CUTOVER"


def test_data_corruption_detected_by_shadow_comparison(tmp_path):
    _,migrator,_,db=_setup(tmp_path)
    migrator.verify_or_stage()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TRIGGER shadow_operations_immutable")
        connection.execute("UPDATE shadow_migration_operations SET record_sha256='bad'")
    with pytest.raises(ShadowMigrationHold,match="shadow rows drifted"):
        migrator.inspect()


def test_shadow_coexists_with_actual_sqlite_budget_without_settlement(tmp_path):
    budget_dir=tmp_path/"budget"
    backend=SQLiteConsensusBackend(str(budget_dir))
    assert backend.register_resource("budget",100)=="REGISTERED"
    reserved=backend.reserve("op-1",10,"budget")
    assert reserved.status=="RESERVED"
    registry,migrator,source,db=_setup(tmp_path,budget_dir/"consensus.sqlite3")
    report=migrator.verify_or_stage()
    assert report.execution_authorized is False
    budget=SQLiteConsensusBackend(str(budget_dir)).get_state("budget")
    assert str(budget.reserved)=="10"
    assert str(budget.committed)=="0"
    assert registry.get("op-1").state==OperationState.UNKNOWN


def test_reject_in_memory_or_missing_file(tmp_path):
    with pytest.raises(ShadowMigrationHold,match="persistent"):
        ShadowOperationMigrator(DurableOperationRegistry(),tmp_path/"a.db")
    empty=DurableOperationRegistry(str(tmp_path/"not-created.json"))
    mig=ShadowOperationMigrator(empty,tmp_path/"b.db")
    with pytest.raises(ShadowMigrationHold,match="source file missing"):
        mig.verify_or_stage()


def _child_import(source_path, db_path, ready, results):
    try:
        registry=DurableOperationRegistry(source_path)
        migrator=ShadowOperationMigrator(registry,db_path)
        if not ready.wait(10):
            raise RuntimeError("no start")
        result=migrator.verify_or_stage()
        results.put(("ok",result.snapshot_sha256))
    except BaseException as exc:
        results.put(("error",type(exc).__name__,str(exc)))


def test_two_processes_import_one_identical_epoch(tmp_path):
    _,migrator,source,db=_setup(tmp_path)
    ctx=multiprocessing.get_context("spawn")
    ready,results=ctx.Event(),ctx.Queue()
    workers=[ctx.Process(target=_child_import,args=(str(source),str(db),ready,results))
             for _ in range(2)]
    for process in workers:
        process.start()
    ready.set()
    outcome=[results.get(timeout=25) for _ in workers]
    for process in workers:
        process.join(timeout=15)
        assert process.exitcode==0
    assert all(entry[0]=="ok" for entry in outcome),outcome
    assert outcome[0][1]==outcome[1][1]
    assert migrator.inspect().snapshot_sha256==outcome[0][1]
    assert _counts(db)==(1,1)


def test_duplicate_top_level_key_fails_without_any_shadow_write(tmp_path):
    _,migrator,source,db=_setup(tmp_path)
    original=source.read_text(encoding="utf-8")
    encoded=json.loads(original)["op-1"]
    source.write_text('{"op-1":'+json.dumps(encoded)+',"op-1":'+json.dumps(encoded)+'}',
                      encoding="utf-8")
    with pytest.raises(ShadowMigrationHold,match="duplicate JSON"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)


def test_duplicate_nested_key_fails_even_when_json_parser_would_ignore_it(tmp_path):
    _,migrator,source,db=_setup(tmp_path)
    encoded=json.dumps(json.loads(source.read_text())["op-1"])
    # Duplicate a nested scalar without changing outer member count.
    assert '"agent_id": "agent-1"' in encoded
    encoded=encoded.replace('"agent_id": "agent-1"',
                            '"agent_id": "agent-1", "agent_id": "intruder"')
    source.write_text('{"op-1":'+encoded+'}',encoding="utf-8")
    with pytest.raises(ShadowMigrationHold,match="duplicate JSON"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)


def test_non_finite_numeric_source_fails_without_shadow_epoch(tmp_path):
    _,migrator,source,db=_setup(tmp_path)
    raw=source.read_text(encoding="utf-8")
    assert '"amount": 10.0' in raw
    source.write_text(raw.replace('"amount": 10.0','"amount": NaN'),encoding="utf-8")
    with pytest.raises(ShadowMigrationHold,match="non-finite"):
        migrator.verify_or_stage()
    assert _counts(db)==(0,0)
