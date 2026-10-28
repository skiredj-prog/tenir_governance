"""W2.1 tests: persistence, restart, idempotence conflict"""
import os, tempfile, json, sqlite3
from lab_s1_bridge import run_lab_s1_scenario, RC01Lab
from proof_provider import ProofProvider

def test_restart_persistence():
    with tempfile.TemporaryDirectory() as d:
        r1 = run_lab_s1_scenario(op_id="op-restart", db_dir=d)
        assert r1["execute_count"] == 1
        assert r1["settlement_count"] == 1
        assert r1["final_state"] == "RECONCILED_CONFIRMED"
        # Re-open stores
        provider = ProofProvider(db_path=os.path.join(d, "provider.db"))
        assert provider.execute_count("op-restart") == 1
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        assert rc01.get_state("op-restart") == "RECONCILED_CONFIRMED"
        print("restart persistence (B3 + RC-01): PASS")

def test_idempotence_conflict():
    with tempfile.TemporaryDirectory() as d:
        p = ProofProvider(db_path=os.path.join(d, "p.db"))
        payload1 = {"scope": {"account": "a", "amount_minor": 100, "currency": "EUR"}}
        payload2 = {"scope": {"account": "b", "amount_minor": 200, "currency": "EUR"}}
        p.execute("op1", "same-key", payload1)
        try:
            p.execute("op2", "same-key", payload2)
            raise AssertionError("should have rejected different request on same key")
        except ValueError as e:
            assert "idempotency key conflict" in str(e)
        print("idempotence conflict: PASS")

def test_terminal_not_reverted():
    with tempfile.TemporaryDirectory() as d:
        r1 = run_lab_s1_scenario(op_id="op-terminal", db_dir=d)
        assert r1["final_state"] == "RECONCILED_CONFIRMED"
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        try:
            rc01.transition("op-terminal", "RESERVED", "replay_attempt")
            raise AssertionError("should have refused leaving terminal state")
        except RuntimeError as e:
            assert "refusing terminal replacement" in str(e)
        assert rc01.get_state("op-terminal") == "RECONCILED_CONFIRMED"
        print("terminal state protected: PASS")

def test_terminal_to_terminal_rejected():
    with tempfile.TemporaryDirectory() as d:
        r1 = run_lab_s1_scenario(op_id="op-tt", db_dir=d)
        assert r1["final_state"] == "RECONCILED_CONFIRMED"
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        try:
            rc01.transition("op-tt", "CONFIRMED", "contradictory_terminal")
            raise AssertionError("should have refused terminal-to-terminal")
        except RuntimeError as e:
            assert "refusing terminal replacement" in str(e)
        assert rc01.get_state("op-tt") == "RECONCILED_CONFIRMED"
        print("terminal-to-terminal rejected: PASS")

def test_migration_duplicates_fail():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "legacy.db")
        # Create legacy DB with duplicates
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE effects (
                operation_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT,
                effect_status TEXT,
                receipt_json TEXT,
                signature BLOB,
                created_at TEXT
            )
        """)
        conn.execute("INSERT INTO effects VALUES ('op1', 'dup-key', 'h1', 'EXECUTED', '{}', X'', 't')")
        conn.execute("INSERT INTO effects VALUES ('op2', 'dup-key', 'h2', 'EXECUTED', '{}', X'', 't')")
        conn.commit()
        conn.close()
        try:
            ProofProvider(db_path=db)
            raise AssertionError("should have refused duplicate keys on open")
        except RuntimeError as e:
            assert "invalid migration" in str(e)
        print("migration duplicates fail-closed: PASS")

def test_concurrent_terminal():
    import multiprocessing
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "rc01.db")
        # Seed a non-terminal state
        rc = RC01Lab(db_path=db)
        rc.transition("op-conc", "REPAIR", "seed")
        queue = multiprocessing.Queue()
        def worker(target, q):
            try:
                r = RC01Lab(db_path=db)
                r.transition("op-conc", target, "race")
                q.put(("ok", target))
            except RuntimeError:
                q.put(("rejected", target))
        p1 = multiprocessing.Process(target=worker, args=("RECONCILED_CONFIRMED", queue))
        p2 = multiprocessing.Process(target=worker, args=("FAILED", queue))
        p1.start(); p2.start()
        p1.join(); p2.join()
        results = []
        while not queue.empty():
            results.append(queue.get())
        final = RC01Lab(db_path=db).get_state("op-conc")
        assert final in ("RECONCILED_CONFIRMED", "FAILED")
        ok_count = sum(1 for r in results if r[0] == "ok")
        assert ok_count == 1, f"expected exactly one success, got {results}"
        print(f"concurrent terminal: PASS (winner={final})")

if __name__ == "__main__":
    test_restart_persistence()
    test_idempotence_conflict()
    test_terminal_not_reverted()
    test_terminal_to_terminal_rejected()
    test_migration_duplicates_fail()
    test_concurrent_terminal()
    print("W2.1 tests: PASS")
