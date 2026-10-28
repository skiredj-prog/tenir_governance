"""G4-A: atomic *simulated* settlement in a disposable POSIX SQLite store.

No provider, dispatch grant, production adapter or live migration. The G3
snapshot stays immutable. This class owns a separately versioned current state,
commands and outbox. Quarantine gate -> G3 JSON lock -> SQLite transaction.
SQL guards exclude ordinary direct backend connections, not a privileged SQL
client capable of forging functions or DDL, nor hostile code in this process.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
import hashlib
import json
import os
import sqlite3
import time
import uuid

from .budget import _decimal_add, _decimal_subtract
from .experimental_native_sqlite_authority import ExperimentalSqliteOperationAuthority, SqliteAuthorityHold
from .registry import DurableOperationRegistry, OperationRecord, OperationState
from .sqlite_backend import SQLiteConsensusBackend


@dataclass(frozen=True)
class SimulatedEvidence:
    """Synthetic input, NOT signed provider evidence or an execution authority."""
    operation_key: str
    reservation_id: str
    attempt_id: str
    payload_hash: str
    authority_epoch: str
    trust_generation: int
    outcome: str
    evidence_id: str


@dataclass(frozen=True)
class G4AReceipt:
    operation_key: str
    reservation_id: str
    attempt_id: str
    payload_hash: str
    authority_epoch: str
    trust_generation: int
    operation_state: str
    budget_state: str
    version: int
    event_hash: str
    decision: str = field(default="HOLD", init=False)
    execution_authorized: bool = field(default=False, init=False)
    budget_settlement_authorized: bool = field(default=False, init=False)
    is_production_authority: bool = field(default=False, init=False)
    simulated: bool = field(default=True, init=False)


_TABLES = {
    "resources", "reservations", "compensations", "experimental_native_operations",
    "experimental_native_reservation_intents", "experimental_g3_exclusive_epoch",
}
_G4_TABLES = {"g4a_meta", "g4a_current", "g4a_outbox", "g4a_commands"}
_OUTCOMES = {"UNKNOWN": ("SIMULATED_UNKNOWN", "RESERVED"),
             "EFFECT_CONFIRMED": ("SIMULATED_CONFIRMED", "COMMITTED"),
             "NON_EXECUTION_CONFIRMED": ("SIMULATED_FAILED", "RELEASED")}
_STATE_BUDGET = {"RESERVED":"RESERVED", **{v[0]:v[1] for v in _OUTCOMES.values()}}


def _text(value, name):
    if not isinstance(value,str) or not value.strip() or len(value)>256:
        raise SqliteAuthorityHold("invalid "+name)
    return value


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _receipt(payload):
    values=json.loads(payload) if isinstance(payload,str) else payload
    try:
        result=G4AReceipt(**{k:v for k,v in values.items() if k in G4AReceipt.__dataclass_fields__
                             and G4AReceipt.__dataclass_fields__[k].init})
        if asdict(result)!=values: raise ValueError("receipt fields changed")
        return result
    except (TypeError,ValueError,KeyError,AttributeError) as exc:
        raise SqliteAuthorityHold("invalid persisted simulated receipt") from exc


class ExperimentalG4AAuthority(ExperimentalSqliteOperationAuthority):
    """Explicit lab schema. Do not wire this to DualKernelGovernor/provider."""

    def __init__(self, backend, *, trust_generation, experimental_only=False):
        if experimental_only is not True or os.name!="posix":
            raise SqliteAuthorityHold("explicit POSIX experimental_only=True required")
        if not isinstance(backend,SQLiteConsensusBackend):
            raise SqliteAuthorityHold("SQLite backend required")
        if type(trust_generation) is not int or not 1<=trust_generation<2**63:
            raise SqliteAuthorityHold("invalid trust generation")
        from ..deployment_safety import enforce_host_scope, runtime_environment
        if enforce_host_scope()!="single-host" or runtime_environment() not in {"test","development"}:
            raise SqliteAuthorityHold("G4-A is development/test single-host only")
        # Never call G3's schema-creating constructor when opening a G4 store.
        self.backend=backend
        self.trust_generation=trust_generation
        self.registry=DurableOperationRegistry(self._g3_path())

    @classmethod
    def open(cls, backend, *, trust_generation, experimental_only=False):
        result=cls(backend,trust_generation=trust_generation,experimental_only=experimental_only)
        with result._guard(): pass
        return result

    @classmethod
    def provision(cls, backend, *, trust_generation, experimental_only=False):
        """Offline, empty, activated G3 lab store only; never auto-upgrade."""
        result=cls(backend,trust_generation=trust_generation,experimental_only=experimental_only)
        with result._guard(provision=True) as (db,epoch,source):
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names!=_TABLES or db.execute("SELECT 1 FROM reservations LIMIT 1").fetchone():
                raise SqliteAuthorityHold("provision requires an empty G3 laboratory schema")
            if db.execute("SELECT 1 FROM experimental_native_operations LIMIT 1").fetchone():
                raise SqliteAuthorityHold("existing admission cannot be migrated")
            for r in db.execute("SELECT resource_id FROM resources"):
                result._verify_budget_locked(db,r[0])
            statements=[
                "CREATE TABLE g4a_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL CHECK(schema_version=1), authority_epoch TEXT NOT NULL, trust_generation INTEGER NOT NULL, schema_digest TEXT NOT NULL)",
                "CREATE TABLE g4a_current(operation_key TEXT PRIMARY KEY REFERENCES experimental_native_operations(operation_key), reservation_id TEXT NOT NULL UNIQUE REFERENCES reservations(reservation_id), attempt_id TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL, authority_epoch TEXT NOT NULL, trust_generation INTEGER NOT NULL, operation_state TEXT NOT NULL CHECK(operation_state IN ('RESERVED','SIMULATED_UNKNOWN','SIMULATED_CONFIRMED','SIMULATED_FAILED')), version INTEGER NOT NULL CHECK(version>0), event_hash TEXT NOT NULL)",
                "CREATE TABLE g4a_outbox(operation_key TEXT NOT NULL REFERENCES g4a_current(operation_key), version INTEGER NOT NULL, event_hash TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL, PRIMARY KEY(operation_key,version))",
                "CREATE TABLE g4a_commands(request_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL REFERENCES g4a_current(operation_key), input_digest TEXT NOT NULL, input_json TEXT NOT NULL, receipt_json TEXT NOT NULL, version INTEGER NOT NULL, UNIQUE(operation_key,version))",
                # Explicit replacement within the same schema transaction.
                "DROP TRIGGER experimental_g3_no_budget_transition",
                "CREATE TRIGGER experimental_g3_no_budget_transition BEFORE UPDATE ON reservations WHEN tenir_g4a_write()!=1 BEGIN SELECT RAISE(ABORT,'G4-A guarded transaction required'); END",
            ]
            for sql in statements: db.execute(sql)
            for table in sorted(_TABLES|_G4_TABLES):
                for action in ("INSERT","UPDATE","DELETE"):
                    db.execute(f"CREATE TRIGGER g4a_guard_{table}_{action.lower()} BEFORE {action} ON {table} WHEN tenir_g4a_write()!=1 BEGIN SELECT RAISE(ABORT,'G4-A guarded transaction required'); END")
            for table in ("g4a_outbox","g4a_commands","g4a_meta"):
                for action in ("UPDATE","DELETE"):
                    db.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'G4-A immutable history'); END")
            db.execute("INSERT INTO g4a_meta VALUES(1,1,?,?,?)",(epoch,trust_generation,result._schema_digest(db)))
            result._validate(db,epoch,source)
        return result

    def _before_guard(self):
        """Test seam before acquiring any authority lock; grants nothing."""

    def _fault(self, stage):
        """Process-crash test seam; not a configured callback/provider."""

    @staticmethod
    def _schema_digest(db):
        return _digest([tuple(r) for r in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")])

    @contextmanager
    def _guard(self, *, provision=False):
        self._before_guard()
        try:
            # All participating legacy activation/unlock shares this gate. Hold
            # it THROUGH COMMIT; revalidate quarantine inside BEGIN IMMEDIATE.
            with self.registry.quarantine_guard():
                with super()._g3_transaction(write=True) as (db,epoch,source):
                    if self.registry._quarantine_read_locked().get("active") is not False:
                        raise SqliteAuthorityHold("durable quarantine active or unknown: HOLD")
                    if not provision:self._validate(db,epoch,source)
                    # Connection-local, never caller-supplied. Not a security
                    # boundary against hostile Python or privileged SQL/DDL.
                    db.create_function("tenir_g4a_write",0,lambda:1)
                    yield db,epoch,source
                    if self.registry._quarantine_read_locked().get("active") is not False:
                        raise SqliteAuthorityHold("durable quarantine changed before commit: HOLD")
                    self._validate(db,epoch,source)
                    self._fault("before_commit")
            self._fault("after_commit")
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,ArithmeticError) as exc:
            raise SqliteAuthorityHold("G4-A integrity/transaction unavailable: "+str(exc)) from exc

    def _validate(self, db, epoch, source):
        meta=db.execute("SELECT * FROM g4a_meta WHERE singleton=1").fetchone()
        if (meta is None or meta["schema_version"]!=1 or meta["authority_epoch"]!=epoch
                or meta["trust_generation"]!=self.trust_generation
                or meta["schema_digest"]!=self._schema_digest(db)):
            raise SqliteAuthorityHold("G4-A schema, epoch or trust generation mismatch")
        native={r["operation_key"]:r for r in db.execute("SELECT * FROM experimental_native_operations")}
        current={r["operation_key"]:r for r in db.execute("SELECT * FROM g4a_current")}
        reservations={r["idempotency_key"]:r for r in db.execute("SELECT * FROM reservations")}
        intents={r["operation_key"]:r for r in db.execute("SELECT * FROM experimental_native_reservation_intents")}
        if not (native.keys()==current.keys()==reservations.keys()==intents.keys()):
            raise SqliteAuthorityHold("G4-A partial admission/current/intent/reservation")
        if db.execute("SELECT 1 FROM compensations LIMIT 1").fetchone():
            raise SqliteAuthorityHold("compensation is outside this simulated tranche")
        totals={}
        for key,row in current.items():
            if key not in source:raise SqliteAuthorityHold("native operation absent from frozen source")
            original=source[key]; n=native[key]; r=reservations[key]; intent=intents[key]
            _,input_hash,amount=self._input(original)
            expected=replace(original,state=OperationState.RESERVED,budget_reserved=True,reservation_id=r["reservation_id"])
            canonical=self._canonical(expected.to_dict())
            if (n["record_json"].encode("utf-8")!=canonical or n["record_sha256"]!=self._sha(canonical)
                or n["input_sha256"]!=input_hash or n["state"]!="RESERVED"
                or n["reservation_id"]!=r["reservation_id"] or n["authority_epoch"]!=epoch
                or intent["reservation_id"]!=r["reservation_id"] or intent["authority_epoch"]!=epoch
                or intent["input_sha256"]!=input_hash or row["reservation_id"]!=r["reservation_id"]
                or row["authority_epoch"]!=epoch or row["trust_generation"]!=self.trust_generation
                or row["payload_hash"]!=original.payload_hash or not row["attempt_id"].startswith("lab:")
                or r["resource_id"]!=original.resource_id or self.backend._amount(r["amount"])!=amount
                or _STATE_BUDGET.get(row["operation_state"])!=r["state"]):
                raise SqliteAuthorityHold("G4-A immutable identity/current/budget mismatch")
            group=totals.setdefault(r["resource_id"],{"RESERVED":Decimal(0),"COMMITTED":Decimal(0)})
            if r["state"] in group:group[r["state"]]=_decimal_add(group[r["state"]],amount)
            events=db.execute("SELECT * FROM g4a_outbox WHERE operation_key=? ORDER BY version",(key,)).fetchall()
            commands=db.execute("SELECT * FROM g4a_commands WHERE operation_key=? ORDER BY version",(key,)).fetchall()
            if len(events)!=row["version"] or len(commands)!=len(events):
                raise SqliteAuthorityHold("G4-A current/event/command count mismatch")
            previous=""
            for version,(event,command) in enumerate(zip(events,commands),1):
                body=json.loads(event["event_json"]); inputs=json.loads(command["input_json"])
                receipt=_receipt(command["receipt_json"])
                result=asdict(receipt);result.pop("event_hash")
                if (event["version"]!=version or command["version"]!=version
                    or _canonical(body)!=event["event_json"] or _digest(body)!=event["event_hash"]
                    or body!={"schema":"tenir.g4a.event.v1","previous_hash":previous,
                              "request_id":command["request_id"],"input_digest":command["input_digest"],"receipt":result}
                    or _digest(inputs)!=command["input_digest"] or _canonical(inputs)!=command["input_json"]
                    or receipt.event_hash!=event["event_hash"] or receipt.version!=version
                    or receipt.operation_key!=key or receipt.reservation_id!=row["reservation_id"]
                    or receipt.attempt_id!=row["attempt_id"] or receipt.payload_hash!=row["payload_hash"]
                    or receipt.authority_epoch!=epoch or receipt.trust_generation!=self.trust_generation
                    or _STATE_BUDGET.get(receipt.operation_state)!=receipt.budget_state):
                    raise SqliteAuthorityHold("G4-A event/command integrity mismatch")
                previous=event["event_hash"]
            if (previous!=row["event_hash"] or receipt.operation_state!=row["operation_state"]
                    or receipt.budget_state!=r["state"]):
                raise SqliteAuthorityHold("G4-A current state/event mismatch")
        resources={r["resource_id"]:r for r in db.execute("SELECT * FROM resources")}
        if not set(totals)<=resources.keys():raise SqliteAuthorityHold("missing resource")
        for key,r in resources.items():
            numbers=[self.backend._amount(r[k],allow_zero=True) for k in ("capacity","reserved","committed")]
            if not key.startswith("lab:") or any(x is None for x in numbers):raise SqliteAuthorityHold("non-lab or invalid budget")
            capacity,reserved,committed=numbers
            counted=totals.get(key,{"RESERVED":Decimal(0),"COMMITTED":Decimal(0)})
            if reserved!=counted["RESERVED"] or committed!=counted["COMMITTED"] or _decimal_add(reserved,committed)>capacity:
                raise SqliteAuthorityHold("I8 counter/ledger divergence")
        if db.execute("PRAGMA foreign_key_check").fetchone():raise SqliteAuthorityHold("G4-A foreign key mismatch")

    def _event(self,db,row,request_id,inputs,budget_state):
        result=dict(operation_key=row["operation_key"],reservation_id=row["reservation_id"],
            attempt_id=row["attempt_id"],payload_hash=row["payload_hash"],authority_epoch=row["authority_epoch"],
            trust_generation=row["trust_generation"],operation_state=row["operation_state"],budget_state=budget_state,
            version=row["version"],event_hash="")
        fields=asdict(G4AReceipt(**result));fields.pop("event_hash")
        body={"schema":"tenir.g4a.event.v1","previous_hash":row["event_hash"],
              "request_id":request_id,"input_digest":_digest(inputs),"receipt":fields}
        event_hash=_digest(body);result["event_hash"]=event_hash
        receipt=G4AReceipt(**result)
        db.execute("INSERT INTO g4a_outbox VALUES(?,?,?,?)",(row["operation_key"],row["version"],event_hash,_canonical(body)))
        db.execute("INSERT INTO g4a_commands VALUES(?,?,?,?,?,?)",(request_id,row["operation_key"],_digest(inputs),_canonical(inputs),_canonical(asdict(receipt)),row["version"]))
        db.execute("UPDATE g4a_current SET event_hash=? WHERE operation_key=?",(event_hash,row["operation_key"]))
        return receipt

    @staticmethod
    def _replay(db,request_id,inputs):
        prior=db.execute("SELECT * FROM g4a_commands WHERE request_id=?",(request_id,)).fetchone()
        if prior:
            if prior["input_digest"]!=_digest(inputs):raise SqliteAuthorityHold("request ID reused with different inputs")
            return _receipt(prior["receipt_json"])

    def admit_created(self, record, *, attempt_id, request_id):
        key,input_hash,amount=self._input(record)
        _text(request_id,"request ID");_text(attempt_id,"attempt ID")
        if not attempt_id.startswith("lab:"):raise SqliteAuthorityHold("lab attempt required")
        inputs={"kind":"ADMIT","operation":record.to_dict(),"attempt_id":attempt_id}
        with self._guard() as (db,epoch,source):
            if key not in source or source[key].to_dict()!=record.to_dict():raise SqliteAuthorityHold("frozen source mismatch")
            prior=self._replay(db,request_id,inputs)
            if prior:return prior
            if db.execute("SELECT 1 FROM g4a_current WHERE operation_key=?",(key,)).fetchone():
                raise SqliteAuthorityHold("operation already admitted; replay original request")
            r=db.execute("SELECT * FROM resources WHERE resource_id=?",(record.resource_id,)).fetchone()
            if r is None or _decimal_add(_decimal_add(Decimal(r["reserved"]),Decimal(r["committed"])),amount)>Decimal(r["capacity"]):
                raise SqliteAuthorityHold("INSUFFICIENT_BUDGET")
            rid=uuid.uuid4().hex
            db.execute("INSERT INTO experimental_native_reservation_intents VALUES(?,?,?,?)",(key,rid,epoch,input_hash))
            db.execute("INSERT INTO reservations(reservation_id,idempotency_key,resource_id,amount,state,created_at) VALUES(?,?,?,?,'RESERVED',?)",(rid,key,record.resource_id,str(amount),time.time()))
            db.execute("UPDATE resources SET reserved=? WHERE resource_id=?",(str(_decimal_add(Decimal(r["reserved"]),amount)),record.resource_id))
            self._fault("after_budget")
            frozen=replace(record,state=OperationState.RESERVED,budget_reserved=True,reservation_id=rid)
            canonical=self._canonical(frozen.to_dict())
            db.execute("INSERT INTO experimental_native_operations VALUES(?,?,?,?,?,?,'RESERVED')",(key,input_hash,self._sha(canonical),canonical.decode(),epoch,rid))
            db.execute("INSERT INTO g4a_current VALUES(?,?,?,?,?,?,'RESERVED',1,'')",(key,rid,attempt_id,record.payload_hash,epoch,self.trust_generation))
            row=dict(db.execute("SELECT * FROM g4a_current WHERE operation_key=?",(key,)).fetchone())
            result=self._event(db,row,request_id,inputs,"RESERVED")
        return result

    def settle(self, request_id, evidence):
        _text(request_id,"request ID")
        if not isinstance(evidence,(list,tuple)) or not 1<=len(evidence)<=16:
            raise SqliteAuthorityHold("bounded simulated evidence batch required")
        for item in evidence:
            if type(item) is not SimulatedEvidence:raise SqliteAuthorityHold("synthetic typed evidence required")
            for k,v in asdict(item).items():
                if k!="trust_generation":_text(v,k)
            if type(item.trust_generation) is not int or item.outcome not in _OUTCOMES:
                raise SqliteAuthorityHold("invalid simulated outcome/trust")
        if len({x.evidence_id for x in evidence})!=len(evidence) or len({x.outcome for x in evidence})!=1:
            raise SqliteAuthorityHold("duplicate or contradictory simulated evidence")
        inputs={"kind":"SIMULATED_SETTLEMENT","evidence":sorted([asdict(x) for x in evidence],key=lambda x:x["evidence_id"])}
        with self._guard() as (db,epoch,source):
            row=db.execute("SELECT * FROM g4a_current WHERE operation_key=?",(evidence[0].operation_key,)).fetchone()
            if row is None:raise SqliteAuthorityHold("operation not admitted")
            for item in evidence:
                if any(getattr(item,k)!=row[k] for k in ("operation_key","reservation_id","attempt_id","payload_hash","authority_epoch","trust_generation")):
                    raise SqliteAuthorityHold("simulated evidence binding mismatch")
            prior=self._replay(db,request_id,inputs)
            if prior:return prior
            if row["operation_state"] in {"SIMULATED_CONFIRMED","SIMULATED_FAILED"}:
                last=db.execute("SELECT * FROM g4a_commands WHERE operation_key=? AND version=?",(row["operation_key"],row["version"])).fetchone()
                if last["input_digest"]==_digest(inputs):return _receipt(last["receipt_json"])
                raise SqliteAuthorityHold("contradictory or changed terminal settlement")
            target,budget=_OUTCOMES[evidence[0].outcome]
            reservation=db.execute("SELECT * FROM reservations WHERE reservation_id=?",(row["reservation_id"],)).fetchone()
            if reservation["state"]!="RESERVED":raise SqliteAuthorityHold("reservation no longer owned")
            if budget!="RESERVED":
                resource=db.execute("SELECT * FROM resources WHERE resource_id=?",(reservation["resource_id"],)).fetchone()
                amount=Decimal(reservation["amount"])
                db.execute("UPDATE resources SET reserved=?,committed=? WHERE resource_id=?",(
                    str(_decimal_subtract(Decimal(resource["reserved"]),amount)),
                    str(_decimal_add(Decimal(resource["committed"]),amount) if budget=="COMMITTED" else Decimal(resource["committed"])),reservation["resource_id"]))
                column="committed_at" if budget=="COMMITTED" else "released_at"
                db.execute(f"UPDATE reservations SET state=?,{column}=? WHERE reservation_id=?",(budget,time.time(),row["reservation_id"]))
            self._fault("after_budget")
            db.execute("UPDATE g4a_current SET operation_state=?,version=version+1 WHERE operation_key=?",(target,row["operation_key"]))
            updated=dict(db.execute("SELECT * FROM g4a_current WHERE operation_key=?",(row["operation_key"],)).fetchone())
            result=self._event(db,updated,request_id,inputs,budget)
        return result

    def inspect(self, operation_key):
        _text(operation_key,"operation key")
        with self._guard() as (db,epoch,source):
            row=db.execute("SELECT receipt_json FROM g4a_commands WHERE operation_key=? ORDER BY version DESC LIMIT 1",(operation_key,)).fetchone()
            if row is None:raise SqliteAuthorityHold("operation not admitted")
            return _receipt(row[0])

    def inspect_all(self):
        with self._guard() as (db,epoch,source):
            return [_receipt(r[0]) for r in db.execute("SELECT c.receipt_json FROM g4a_commands c JOIN g4a_current s ON s.operation_key=c.operation_key AND s.version=c.version ORDER BY c.operation_key")]
