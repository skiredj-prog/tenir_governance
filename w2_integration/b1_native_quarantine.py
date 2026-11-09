"""Single-host live quarantine fencing for the experimental native B1 store.

The SQLite transaction is already held when entering this gate. Acquisition
must therefore be nonblocking: on contention roll back, never invert the
blocking Governor gate -> SQLite lock order. Provider and witness I/O must
finish before entering the final commit gate.
"""
from contextlib import contextmanager, ExitStack
import json
import os

from tenir_governance.operations.registry import DurableOperationRegistry, OperationState


class QuarantineGateBusy(RuntimeError):
    """A native transaction must roll back before any bounded local retry."""


def bind_registry(db, registry):
    if type(registry) is not DurableOperationRegistry or not registry.is_persistent:
        raise RuntimeError("persistent native quarantine registry required")
    source = registry._store_path
    path = os.path.normcase(os.path.realpath(source))
    if not os.path.isabs(source) or os.path.normcase(os.path.abspath(source)) != path:
        # A symlinked registry can have different quarantine sidecars from its
        # target. Never silently canonicalize such a different authority.
        raise RuntimeError("native quarantine registry requires an absolute path without symlink aliases")
    db.execute("CREATE TABLE IF NOT EXISTS b1_native_quarantine_registry "
               "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), path TEXT NOT NULL)")
    row = db.execute("SELECT path FROM b1_native_quarantine_registry WHERE singleton=1").fetchone()
    if row is None:
        if db.execute("SELECT 1 FROM w2_positive_v1_permits LIMIT 1").fetchone():
            raise RuntimeError("native quarantine binding missing over existing permits; review required")
        db.execute("INSERT INTO b1_native_quarantine_registry VALUES(1,?)", (path,))
    elif row["path"] != path:
        raise RuntimeError("native quarantine registry binding changed")
    for event in ("UPDATE", "DELETE"):
        db.execute("CREATE TRIGGER IF NOT EXISTS b1_native_quarantine_no_" + event.lower() +
                   " BEFORE " + event + " ON b1_native_quarantine_registry "
                   "BEGIN SELECT RAISE(ABORT, 'native quarantine registry is immutable'); END")


@contextmanager
def commit_guard(db, checks):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='b1_native_quarantine_registry'").fetchone():
        raise RuntimeError("native quarantine registry binding missing; HOLD")
    binding = db.execute("SELECT path FROM b1_native_quarantine_registry WHERE singleton=1").fetchone()
    if not binding or not os.path.isabs(binding["path"]):
        raise RuntimeError("native quarantine registry binding invalid; HOLD")
    path = binding["path"]
    if path != os.path.normcase(os.path.realpath(path)) or not os.path.isfile(path):
        raise RuntimeError("native quarantine registry unavailable; HOLD")
    registry = DurableOperationRegistry(path)
    with ExitStack() as lease:
        try:
            lease.enter_context(registry.quarantine_guard(blocking=False))
        except OSError as exc:
            raise QuarantineGateBusy("native quarantine gate unavailable or busy; HOLD") from exc
        try:
            mark = registry._quarantine_read_locked()
            if mark.get("active") is not False:
                raise RuntimeError("native quarantine active or corrupt; HOLD")
            for proof, payload in checks:
                if mark.get("lock_id", "initial") != proof["quarantine_epoch"]:
                    raise RuntimeError("native quarantine epoch changed; HOLD")
                record = registry.get(proof["operation_key"])
                receipt = json.loads(proof["governor_receipt_json"])
                if (registry.is_corrupted or not os.path.isfile(path) or not record
                        or record.state != OperationState.RESERVED
                        or record.payload_hash != receipt["request_hash"]
                        or record.passage_id != receipt["passage_id"]
                        or record.agent_id != payload["agent_id"]
                        or record.action_type != payload["action_type"]
                        or record.resource_id != payload["resource_id"]
                        or record.amount != payload["amount"]):
                    raise RuntimeError("native quarantine Governor record no longer authorizes request")
        except OSError as exc:
            raise RuntimeError("native quarantine state unavailable; HOLD") from exc
        yield
