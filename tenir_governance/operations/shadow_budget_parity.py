"""G1 preflight: read-only, frozen JSON-operation × SQLite-budget parity.

This is a single-host, non-authoritative reconciliation gate. It holds the
JSON registry writer lock and a SQLite BEGIN IMMEDIATE while observing the
two stores. It does NOT cut over ownership, settle budgets or dispatch.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .registry import OperationRecord
from .shadow_registry_migration import ShadowMigrationHold, ShadowOperationMigrator


@dataclass(frozen=True)
class BudgetParityReport:
    status: str
    operations: int
    reservations: int
    source_snapshot_sha256: str
    budget_snapshot_sha256: str
    execution_authorized: bool = False
    cutover_authorized: bool = False


class ShadowBudgetParityAudit:
    """Immutable G1 budget-parity evidence for one frozen G0/G2 epoch.

    The audit runs against the SAME SQLite db file that owns the existing
    reservations. It deliberately rejects UNKNOWN reservations, missing
    links and unreconciled/orphan budget records rather than inventing
    a budget settlement. It does not create new source operations.
    """

    def __init__(self, shadow: ShadowOperationMigrator):
        if not isinstance(shadow, ShadowOperationMigrator):
            raise ShadowMigrationHold("approved frozen shadow migrator required")
        self.shadow=shadow
        with self.shadow._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shadow_budget_parity_epoch (
                    epoch INTEGER PRIMARY KEY CHECK(epoch=1),
                    source_sha256 TEXT NOT NULL,
                    budget_sha256 TEXT NOT NULL,
                    reservation_count INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS shadow_budget_epoch_immutable
                BEFORE UPDATE ON shadow_budget_parity_epoch
                BEGIN SELECT RAISE(ABORT,'budget parity epoch immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_budget_epoch_no_delete
                BEFORE DELETE ON shadow_budget_parity_epoch
                BEGIN SELECT RAISE(ABORT,'budget parity epoch cannot be deleted'); END;
            """)

    @staticmethod
    def _decimal(value):
        if value is None or isinstance(value,bool):
            raise ShadowMigrationHold("invalid budget decimal")
        try:
            d=Decimal(str(value))
        except (InvalidOperation,TypeError,ValueError) as exc:
            raise ShadowMigrationHold("invalid budget decimal") from exc
        if not d.is_finite() or d < 0 or len(d.as_tuple().digits)>128:
            raise ShadowMigrationHold("unsafe budget decimal")
        return d

    def verify(self) -> BudgetParityReport:
        reg=self.shadow.registry
        # Matches G0/G2 lock order and SQLite writer lock, so the inventory
        # and budget rows are observed together. Future mutations are possible:
        # this is NOT a permanent snapshot/commit authority.
        with reg.quarantine_guard():
            if reg.quarantine_is_active():
                raise ShadowMigrationHold("source quarantine active")
            with reg._lock:
                flock=reg._lock_file()
                try:
                    normalized,source_sha=self.shadow._snapshot_locked()
                    with self.shadow._db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            epoch=db.execute("""
                                SELECT snapshot_sha256,item_count
                                FROM shadow_migration_epoch WHERE epoch=1
                            """).fetchone()
                            if epoch!=(source_sha,len(normalized)):
                                raise ShadowMigrationHold("G0 frozen inventory mismatch")
                            staged=db.execute("""
                                SELECT operation_key,record_sha256,record_json,source_state
                                FROM shadow_migration_operations ORDER BY operation_key
                            """).fetchall()
                            if staged != [(key,*normalized[key]) for key in sorted(normalized)]:
                                raise ShadowMigrationHold("G2 shadow inventory drift")
                            try:
                                resources=db.execute("""
                                    SELECT resource_id,capacity,reserved,committed
                                    FROM resources ORDER BY resource_id
                                """).fetchall()
                                reservations=db.execute("""
                                    SELECT reservation_id,idempotency_key,resource_id,
                                           amount,state FROM reservations
                                    ORDER BY idempotency_key
                                """).fetchall()
                            except Exception as exc:
                                raise ShadowMigrationHold(
                                    "SQLiteConsensusBackend resources/reservations missing"
                                ) from exc
                            resource_by_id={}
                            reserved_sum={}
                            committed_sum={}
                            for resource_id,cap,reserved,committed in resources:
                                if not isinstance(resource_id,str) or not resource_id:
                                    raise ShadowMigrationHold("missing resource identity")
                                cap=self._decimal(cap)
                                reserved=self._decimal(reserved)
                                committed=self._decimal(committed)
                                if reserved+committed>cap:
                                    raise ShadowMigrationHold("I8 resource counter exceeds capacity")
                                resource_by_id[resource_id]=(cap,reserved,committed)
                                reserved_sum[resource_id]=Decimal(0)
                                committed_sum[resource_id]=Decimal(0)
                            budget_rows=[]
                            found=set()
                            for rid,key,resource,amount,state in reservations:
                                if key in found:
                                    raise ShadowMigrationHold("duplicate reservation operation key")
                                found.add(key)
                                if key not in normalized or resource not in resource_by_id:
                                    raise ShadowMigrationHold("orphan budget reservation/source")
                                source=OperationRecord.from_dict(
                                    __import__("json").loads(normalized[key][1])
                                )
                                quantity=self._decimal(amount)
                                if (source.reservation_id != rid
                                        or source.resource_id != resource
                                        or self._decimal(source.amount) != quantity):
                                    raise ShadowMigrationHold("reservation identity/amount mismatch")
                                if state=="RESERVED":
                                    if not source.budget_reserved or source.budget_committed:
                                        raise ShadowMigrationHold("source flags contradict reserved budget")
                                    reserved_sum[resource]+=quantity
                                elif state=="COMMITTED":
                                    if not source.budget_committed:
                                        raise ShadowMigrationHold("source flags contradict committed budget")
                                    committed_sum[resource]+=quantity
                                elif state=="RELEASED":
                                    if source.budget_reserved or source.budget_committed:
                                        raise ShadowMigrationHold("source flags contradict released budget")
                                else:
                                    # UNKNOWN / REFUNDED / unreviewed states may demand a
                                    # compensation protocol, not a routine cutover.
                                    raise ShadowMigrationHold("unresolved budget reservation lifecycle")
                                budget_rows.append([rid,key,resource,str(quantity),state])
                            for key,(rec_hash,payload,_state) in normalized.items():
                                source=OperationRecord.from_dict(__import__("json").loads(payload))
                                if key not in found and (source.reservation_id or
                                                         source.budget_reserved or
                                                         source.budget_committed):
                                    raise ShadowMigrationHold("operation budget linkage is missing")
                            for resource,(cap,reserved,committed) in resource_by_id.items():
                                if reserved!=reserved_sum[resource] or committed!=committed_sum[resource]:
                                    raise ShadowMigrationHold("I8 counters disagree with reservations")
                            digest=hashlib.sha256(self.shadow._canonical([
                                ["resources",[
                                    [rid,str(self._decimal(cap)),str(self._decimal(res)),
                                     str(self._decimal(comm))]
                                    for rid,cap,res,comm in resources]],
                                ["reservations",budget_rows],
                            ])).hexdigest()
                            prior=db.execute("""
                                SELECT source_sha256,budget_sha256,reservation_count
                                FROM shadow_budget_parity_epoch WHERE epoch=1
                            """).fetchone()
                            expected=(source_sha,digest,len(reservations))
                            if prior is None:
                                db.execute("""INSERT INTO shadow_budget_parity_epoch
                                              (epoch,source_sha256,budget_sha256,reservation_count)
                                              VALUES(1,?,?,?)""",expected)
                            elif prior!=expected:
                                raise ShadowMigrationHold("budget parity changed since frozen baseline")
                            self._before_commit()
                            db.execute("COMMIT")
                        except BaseException:
                            if db.in_transaction:
                                db.execute("ROLLBACK")
                            raise
                    return BudgetParityReport(
                        status="BUDGET_PARITY_MATCHED_NO_CUTOVER",
                        operations=len(normalized),reservations=len(reservations),
                        source_snapshot_sha256=source_sha,
                        budget_snapshot_sha256=digest,
                    )
                finally:
                    if flock:
                        flock.close()

    def _before_commit(self):
        """Fault-injection seam for rollback tests."""
