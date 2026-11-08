"""W2.1 invariants through authenticated native claims; legacy entrypoint stays closed."""
from contextlib import closing
import multiprocessing
import os
import sqlite3
import tempfile

import pytest

from lab_s1_bridge import RC01Lab
from proof_provider import ProofProvider
from w2_regression_support import RESOURCE, command, native_lab, native_s1_scenario


def test_restart_persistence():
    with tempfile.TemporaryDirectory() as d:
        result = native_s1_scenario(d, "op-restart")
        assert result["execute_count"] == 1
        assert result["settlement_count"] == 1
        assert result["final_state"] == "RECONCILED_CONFIRMED"
        with native_lab(d) as reopened:
            assert reopened.provider.execute_count("op-restart") == 1
            assert reopened.provider.verify_receipt(result["receipt"])
        with closing(RC01Lab(os.path.join(d, "rc01.db")).conn) as db:
            assert db.execute("SELECT state FROM rc01_states WHERE operation_id='op-restart'").fetchone()[0] == "RECONCILED_CONFIRMED"


def test_idempotence_conflict():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        first, second = command(100, "a"), command(200, "b")
        lab.auth.admit_reserve_issue("op1", "eval1", first, resource_id=RESOURCE)
        lab.auth.admit_reserve_issue("op2", "eval2", second, resource_id=RESOURCE)
        result = lab.provider.dispatch_authorized(lab.auth, "op1", "same-key", first)
        assert result["status"] == "CONFIRMED"
        with pytest.raises(RuntimeError, match="idempotency key conflict"):
            lab.provider.dispatch_authorized(lab.auth, "op2", "same-key", second)
        assert lab.provider.execute_count("op1") == 1
        assert lab.provider.execute_count("op2") == 0


def test_terminal_not_reverted():
    with tempfile.TemporaryDirectory() as d:
        result = native_s1_scenario(d, "op-terminal")
        assert result["final_state"] == "RECONCILED_CONFIRMED"
        rc01 = RC01Lab(os.path.join(d, "rc01.db"))
        with closing(rc01.conn):
            with pytest.raises(RuntimeError, match="refusing terminal replacement"):
                rc01.transition("op-terminal", "RESERVED", "replay_attempt")
            assert rc01.get_state("op-terminal") == "RECONCILED_CONFIRMED"


def test_terminal_to_terminal_rejected():
    with tempfile.TemporaryDirectory() as d:
        result = native_s1_scenario(d, "op-tt")
        assert result["final_state"] == "RECONCILED_CONFIRMED"
        rc01 = RC01Lab(os.path.join(d, "rc01.db"))
        with closing(rc01.conn):
            with pytest.raises(RuntimeError, match="refusing terminal replacement"):
                rc01.transition("op-tt", "CONFIRMED", "contradictory_terminal")
            assert rc01.get_state("op-tt") == "RECONCILED_CONFIRMED"


def _legacy_database(path, index_sql=None, duplicates=False):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE effects (operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, "
            "request_hash TEXT, effect_status TEXT, receipt_json TEXT, signature BLOB, created_at TEXT)"
        )
        if duplicates:
            db.executemany("INSERT INTO effects VALUES (?, 'dup-key', ?, 'EXECUTED', '{}', X'', 't')",
                           [("op1", "h1"), ("op2", "h2")])
        if index_sql:
            db.execute(index_sql)


def _assert_migration_refused(path, reason):
    # Keep the partially initialized object to close its SQLite handle even when
    # the constructor rejects a corrupt old schema (important on Windows).
    provider = ProofProvider.__new__(ProofProvider)
    try:
        with pytest.raises(RuntimeError, match=reason):
            provider.__init__(db_path=path)
    finally:
        if hasattr(provider, "conn"):
            provider.conn.close()


def test_migration_duplicates_fail():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        _legacy_database(db, duplicates=True)
        _assert_migration_refused(db, "invalid migration")


def test_migration_nonunique_index_fail():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy_nonunique.db")
        _legacy_database(db, "CREATE INDEX idx_effects_idempotency ON effects(idempotency_key)")
        _assert_migration_refused(db, "not UNIQUE")


def test_migration_partial_unique_index_fail():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy_partial.db")
        _legacy_database(db, "CREATE UNIQUE INDEX idx_effects_idempotency ON effects(idempotency_key) WHERE effect_status='EXECUTED'")
        _assert_migration_refused(db, "partial")


def _terminal_worker(path, target, ready, start, results):
    rc = RC01Lab(path)
    try:
        ready.put(target)
        if not start.wait(30):
            raise RuntimeError("terminal race start not observed")
        try:
            rc.transition("op-conc", target, "race")
            results.put(("ok", target))
        except RuntimeError as exc:
            results.put(("rejected", str(exc)))
    finally:
        rc.conn.close()


def test_concurrent_terminal():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "rc01.db")
        rc = RC01Lab(db)
        with closing(rc.conn):
            rc.transition("op-conc", "REPAIR", "seed")
        ctx = multiprocessing.get_context("spawn")
        ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        targets = ["RECONCILED_CONFIRMED", "FAILED"]
        workers = [ctx.Process(target=_terminal_worker, args=(db, state, ready, start, results)) for state in targets]
        for worker in workers:
            worker.start()
        try:
            assert sorted([ready.get(timeout=30), ready.get(timeout=30)]) == sorted(targets)
            start.set()
            outcomes = [results.get(timeout=30), results.get(timeout=30)]
            for worker in workers:
                worker.join(10)
                assert worker.exitcode == 0
            assert sum(status == "ok" for status, _ in outcomes) == 1
            rejected = [reason for status, reason in outcomes if status == "rejected"]
            assert len(rejected) == 1 and "refusing terminal replacement" in rejected[0]
            rc = RC01Lab(db)
            with closing(rc.conn):
                assert rc.get_state("op-conc") == next(state for status, state in outcomes if status == "ok")
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(10)
            ready.close()
            results.close()
