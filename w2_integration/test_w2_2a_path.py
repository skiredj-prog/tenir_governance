"""W2.2a-ter: gate blocks before reservation; no orphan reservation"""
import tempfile, os
from w2_2_integrated import run_integrated_candidate
from proof_provider import ProofProvider
from i8_ledger import I8Ledger

def test_gate_blocks_before_reservation():
    with tempfile.TemporaryDirectory() as d:
        result = run_integrated_candidate(d, "op-blocked")
        assert result["dispatch_refused"] is True
        assert result["refuse_reason"] == "execution_not_authorized"
        assert result["provider_called"] is False
        assert result["execute_count"] == 0
        assert result["reservation_count"] == 0
        # Verify no row in provider or I8
        provider = ProofProvider(db_path=os.path.join(d, "provider.db"))
        assert provider.execute_count("op-blocked") == 0
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        assert i8.reservation_count("op-blocked") == 0
        assert i8.net_settlement_count("op-blocked") == 0
        print("gate blocks before reservation: PASS")

def test_repeated_refusals_no_capacity_consumption():
    with tempfile.TemporaryDirectory() as d:
        for i in range(3):
            r = run_integrated_candidate(d, f"op-repeat-{i}")
            assert r["dispatch_refused"] is True
            assert r["reservation_count"] == 0
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        for i in range(3):
            assert i8.reservation_count(f"op-repeat-{i}") == 0
        print("repeated refusals: PASS")

if __name__ == "__main__":
    test_gate_blocks_before_reservation()
    test_repeated_refusals_no_capacity_consumption()
    print("W2.2a-ter tests: PASS")
