"""W2.1 tests: persistence, restart, idempotence conflict"""
import os, tempfile, json
from lab_s1_bridge import run_lab_s1_scenario
from proof_provider import ProofProvider

def test_restart_persistence():
    with tempfile.TemporaryDirectory() as d:
        r1 = run_lab_s1_scenario(op_id="op-restart", db_dir=d)
        assert r1["execute_count"] == 1
        assert r1["settlement_count"] == 1
        # Re-open stores
        provider = ProofProvider(db_path=os.path.join(d, "provider.db"))
        assert provider.execute_count("op-restart") == 1
        print("restart persistence: PASS")

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

if __name__ == "__main__":
    test_restart_persistence()
    test_idempotence_conflict()
    print("W2.1 tests: PASS")
