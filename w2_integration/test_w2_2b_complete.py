"""W2.2b completion: cross-boundary hash equality and REPAIR no-release"""
import tempfile, os
from w2_2b_canonical import request_hash
from proof_provider import ProofProvider
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab

def test_cross_boundary_hash_equality():
    payload = {"scope": {"account": "a", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
    h_eval = request_hash(payload)
    with tempfile.TemporaryDirectory() as d:
        p = ProofProvider(db_path=os.path.join(d, "p.db"))
        # B3 must compute the same hash
        rec = p.execute("op1", "idem1", payload)
        assert rec["request_hash"] == h_eval
        print("cross-boundary hash equality: PASS")

def test_divergent_hash_refused():
    with tempfile.TemporaryDirectory() as d:
        p = ProofProvider(db_path=os.path.join(d, "p.db"))
        p.execute("op1", "same-key", {"amount": 100})
        try:
            p.execute("op2", "same-key", {"amount": 200})
            raise AssertionError("should have refused divergent request")
        except ValueError as e:
            assert "idempotency key conflict" in str(e)
        print("divergent hash refused: PASS")

def test_no_release_on_repair():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        i8.reserve("op-repair", 100)
        i8.fail_next_settle()
        try:
            i8.settle("op-repair")
        except RuntimeError:
            pass
        # Attempt release while unsettled (REPAIR window)
        try:
            i8.release("op-repair")
            raise AssertionError("should have refused release in REPAIR")
        except PermissionError as e:
            assert "REPAIR" in str(e) or "release refused" in str(e)
        assert i8.net_settlement_count("op-repair") == 0
        print("no release on REPAIR: PASS")

if __name__ == "__main__":
    test_cross_boundary_hash_equality()
    test_divergent_hash_refused()
    test_no_release_on_repair()
    print("W2.2b completion tests: PASS")
