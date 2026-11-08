"""Exact grant issuance replay; no provider execution or automatic retry."""
from contextlib import contextmanager
from copy import deepcopy
import multiprocessing
import queue
import traceback

import pytest

import native_positive_permit as permit_module
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority, _canonical
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OP = "lab:grant-replay"
RESOURCE = "lab:budget"
COMMAND = {"schema": "tenir-dispatch-command/v1", "resource_id": RESOURCE,
           "amount": 7, "action_type": "payment", "agent_id": "lab:agent",
           "metadata": {"recipient": "lab:recipient"}}


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path))
    assert backend.register_resource(RESOURCE, 50) == "REGISTERED"
    return tmp_path, backend, NativePositiveG4A(NativePositivePermitAuthority(backend))


def issue(gate, command=None, evaluation="lab:eval"):
    return gate.evaluate_and_grant(OP, evaluation, deepcopy(COMMAND if command is None else command),
                                   resource_id=RESOURCE)


def test_exact_replay_returns_original_signed_grant_after_reopen(system):
    path, backend, gate = system
    first = issue(gate)
    second = issue(gate)
    reopened = NativePositiveG4A(NativePositivePermitAuthority(SQLiteConsensusBackend(str(path))))
    third = issue(reopened)
    assert first == second == third
    assert backend.get_state(RESOURCE).reserved == 7
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM w2_positive_g4a_v1_grants").fetchone()[0] == 1


def test_changed_payload_and_evaluation_refuse_without_mutation(system):
    _, backend, gate = system
    first = issue(gate)
    changed = deepcopy(COMMAND)
    changed["metadata"]["recipient"] = "other"
    with pytest.raises(RuntimeError):
        issue(gate, changed)
    with pytest.raises(RuntimeError):
        issue(gate, evaluation="lab:other-eval")
    with backend._connection() as db:
        row = db.execute("SELECT grant_id, consumed FROM w2_positive_g4a_v1_grants").fetchone()
        assert tuple(row) == (first["grant_id"], 0)


def test_interrupted_before_grant_insert_can_issue_once_on_exact_retry(system, monkeypatch):
    _, backend, gate = system
    original = gate.authority._write
    @contextmanager
    def fail_once(operation):
        if operation == "positive_g4a_grant":
            raise RuntimeError("injected grant insert interruption")
        with original(operation) as db:
            yield db
    monkeypatch.setattr(gate.authority, "_write", fail_once)
    with pytest.raises(RuntimeError, match="injected"):
        issue(gate)
    monkeypatch.setattr(gate.authority, "_write", original)
    result = issue(gate)
    assert result["amount"] == "7"
    assert backend.get_state(RESOURCE).reserved == 7
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM w2_positive_g4a_v1_grants").fetchone()[0] == 1


def test_consumed_permit_without_grant_refuses_fresh_grant(system):
    _, backend, gate = system
    gate.authority.admit_reserve_issue(OP, "lab:eval", deepcopy(COMMAND), resource_id=RESOURCE)
    # Fault injection models an already consumed permit with no grant.
    with backend._connection() as db:
        db.execute("UPDATE w2_positive_v1_permits SET consumed=1, nonce_status='CONSUMED' WHERE operation_key=?", (OP,))
    with pytest.raises(RuntimeError, match="consumed permit"):
        issue(gate)
    with backend._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM w2_positive_g4a_v1_grants").fetchone()[0] == 0


def test_damaged_stored_grant_signature_refuses_replay(system):
    _, backend, gate = system
    issue(gate)
    # Fault injection simulates a privileged storage mutation, outside the UID boundary.
    with backend._connection() as db:
        db.execute("UPDATE w2_positive_g4a_v1_grants SET signature=?", (bytes(64),))
    with pytest.raises(RuntimeError, match="(?i)signature|integrity"):
        issue(gate)


@pytest.mark.parametrize("column,value", [
    ("resource_id", "lab:other"), ("reservation_id", "res:other"),
    ("amount", "8"), ("expires_at", "2099-01-01T00:00:00+00:00"),
    ("schema_id", "other"),
])
def test_damaged_stored_grant_binding_refuses_replay(system, column, value):
    _, backend, gate = system
    issue(gate)
    # Column name is from a fixed test-controlled list; no user SQL is accepted.
    with backend._connection() as db:
        db.execute(f"UPDATE w2_positive_g4a_v1_grants SET {column}=?", (value,))
    with pytest.raises(RuntimeError, match="(?i)signature|integrity"):
        issue(gate)


def test_replay_preserves_preexisting_signed_decimal_format(system):
    _, backend, gate = system
    first = issue(gate)
    body = {key: value for key, value in first.items() if key not in ("signature", "causal")}
    body["amount"] = "7.0"
    signature = gate.authority.private_key.sign(_canonical(body))
    with backend._connection() as db:
        db.execute("UPDATE w2_positive_g4a_v1_grants SET amount=?, signature=?", ("7.0", signature))
    replay = issue(gate)
    assert replay["amount"] == "7.0" and replay["signature"] == signature
    assert replay["grant_id"] == first["grant_id"]


@pytest.mark.parametrize("damage", ["delete", "change_public_key"])
def test_replay_rejects_missing_or_inconsistent_custody_pin(system, damage):
    _, backend, gate = system
    issue(gate)
    with backend._connection() as db:
        if damage == "delete":
            db.execute("DELETE FROM w2_positive_g4a_v1_custody")
        else:
            db.execute("UPDATE w2_positive_g4a_v1_custody SET public_key_hex=?", ("00" * 32,))
    with pytest.raises(RuntimeError, match="custody integrity"):
        issue(gate)


def test_schema_trigger_replacement_is_invisible_until_commit_and_rolls_back(system, monkeypatch):
    _, backend, _ = system
    trigger = "w2_block_historical_after_positive"
    def trigger_sql(db):
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone()
        return row[0] if row else None
    with backend._connection() as reader:
        original = trigger_sql(reader)
    assert original
    execute = permit_module._execute_schema_statements
    def fail_after_drop(db, script):
        if "DROP TRIGGER IF EXISTS w2_block_historical_after_positive" in script:
            db.execute("DROP TRIGGER w2_block_historical_after_positive")
            assert trigger_sql(db) is None
            with backend._connection() as reader:
                assert trigger_sql(reader) == original
            raise RuntimeError("injected schema interruption")
        execute(db, script)
    monkeypatch.setattr(permit_module, "_execute_schema_statements", fail_after_drop)
    with pytest.raises(RuntimeError, match="injected schema interruption"):
        NativePositivePermitAuthority(backend)
    with backend._connection() as reader:
        assert trigger_sql(reader) == original


def _issue_worker(path, ready, start, results):
    initialized = False
    try:
        gate = NativePositiveG4A(NativePositivePermitAuthority(SQLiteConsensusBackend(path)))
        ready.put("ready")
        initialized = True
        if not start.wait(55):
            raise RuntimeError("start gate timed out")
        grant = issue(gate)
        results.put(("ok", grant["grant_id"], grant["signature"].hex()))
    except BaseException:
        failure = ("error", traceback.format_exc())
        if not initialized:
            ready.put(failure)
        results.put(failure)


def test_two_processes_issue_one_identical_grant(system):
    path, backend, _ = system
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_issue_worker, args=(str(path), ready, start, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        initializations = [ready.get(timeout=45), ready.get(timeout=45)]
        assert initializations == ["ready", "ready"], initializations
        start.set()
        outcomes = [results.get(timeout=30), results.get(timeout=30)]
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0, outcomes
        assert outcomes[0] == outcomes[1] and outcomes[0][0] == "ok", outcomes
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM w2_positive_g4a_v1_grants").fetchone()[0] == 1
    except queue.Empty:
        pytest.fail("worker timed out; exit codes: " + repr([worker.exitcode for worker in workers]))
    finally:
        start.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(15)
        for channel in (ready, results):
            channel.close()
            channel.join_thread()
