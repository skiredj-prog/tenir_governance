"""G1 shadow budget-parity preflight, distinct from production cutover."""
from decimal import Decimal
import sqlite3
import pytest

from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord, OperationState
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.shadow_registry_migration import (
    ShadowOperationMigrator, ShadowMigrationHold,
)
from tenir_governance.operations.shadow_budget_parity import ShadowBudgetParityAudit


def _setup(tmp_path, *, missing_link=False, wrong_amount=False):
    budget_dir=tmp_path/"budget"
    backend=SQLiteConsensusBackend(str(budget_dir))
    assert backend.register_resource("budget",Decimal("100"))=="REGISTERED"
    reservation=backend.reserve("op-budget",Decimal("20"),"budget")
    assert reservation.status=="RESERVED"
    source=tmp_path/"operations.json"
    registry=DurableOperationRegistry(str(source))
    assert registry.register(OperationRecord(
        idempotency_key="op-budget", passage_id="p1", agent_id="alice",
        action_type="payment",resource_id="budget",
        amount=25.0 if wrong_amount else 20.0,
        state=OperationState.UNKNOWN,
        reservation_id="" if missing_link else reservation.reservation_id,
        budget_reserved=True,
    ))
    shadow=ShadowOperationMigrator(registry,budget_dir/"consensus.sqlite3")
    shadow.verify_or_stage()
    audit=ShadowBudgetParityAudit(shadow)
    return audit,registry,backend,source,budget_dir/"consensus.sqlite3",reservation


def test_strict_parity_matches_frozen_source_and_budget(tmp_path):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    before=source.read_bytes()
    report=audit.verify()
    assert report.status=="BUDGET_PARITY_MATCHED_NO_CUTOVER"
    assert report.operations==1 and report.reservations==1
    assert report.execution_authorized is False
    assert report.cutover_authorized is False
    assert audit.verify()==report
    assert source.read_bytes()==before
    assert backend.get_state("budget").reserved==Decimal("20")


def test_budget_commit_after_parity_must_not_silently_rebaseline(tmp_path):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    audit.verify()
    assert backend.commit(res.reservation_id).status=="COMMITTED"
    with pytest.raises(ShadowMigrationHold,match="source flags"):
        audit.verify()


def test_missing_source_reservation_link_fails_closed(tmp_path):
    audit,*_=_setup(tmp_path,missing_link=True)
    with pytest.raises(ShadowMigrationHold,match="reservation identity"):
        audit.verify()


def test_source_amount_mismatch_fails_closed(tmp_path):
    audit,*_=_setup(tmp_path,wrong_amount=True)
    with pytest.raises(ShadowMigrationHold,match="reservation identity"):
        audit.verify()


def test_missing_budget_resource_and_orphan_source_are_rejected(tmp_path):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE reservations SET idempotency_key='unowned'")
    with pytest.raises(ShadowMigrationHold,match="orphan"):
        audit.verify()


def test_corrupt_i8_resource_accounting_rejected(tmp_path):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE resources SET reserved='500' WHERE resource_id='budget'")
    with pytest.raises(ShadowMigrationHold,match="I8"):
        audit.verify()


def test_reservation_unknown_is_not_treated_as_safe_cutover(tmp_path):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE reservations SET state='UNKNOWN' WHERE reservation_id=?",
                           (res.reservation_id,))
    with pytest.raises(ShadowMigrationHold,match="unresolved"):
        audit.verify()


def test_parity_failure_before_sqlite_commit_is_rolled_back(tmp_path,monkeypatch):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    def fail():
        raise RuntimeError("injected G1 rollback")
    monkeypatch.setattr(audit,"_before_commit",fail)
    with pytest.raises(RuntimeError,match="injected"):
        audit.verify()
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM shadow_budget_parity_epoch"
        ).fetchone()[0]==0
    monkeypatch.setattr(audit,"_before_commit",lambda:None)
    assert audit.verify().status=="BUDGET_PARITY_MATCHED_NO_CUTOVER"


def test_shadow_budget_epoch_cannot_be_rewritten(tmp_path):
    audit,registry,backend,source,db,res=_setup(tmp_path)
    report=audit.verify()
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.DatabaseError,match="immutable"):
            connection.execute("UPDATE shadow_budget_parity_epoch SET budget_sha256='tampered'")
        with pytest.raises(sqlite3.DatabaseError,match="cannot"):
            connection.execute("DELETE FROM shadow_budget_parity_epoch")
    assert audit.verify()==report
