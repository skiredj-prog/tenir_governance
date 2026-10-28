"""W2.2 positive contract tests"""
import tempfile, os
from w2_2_positive_g4a import PositiveG4A
from w2_2a_authority import NativeAuthorityGate
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from proof_provider import ProofProvider

def test_positive_sequence():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        provider = ProofProvider(db_path=os.path.join(d, "p.db"))
        g4a = PositiveG4A(i8, rc01, provider)
        payload = {"scope": {"account": "a", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
        permit = g4a.admit_and_reserve("op-pos", "eval1", payload)
        assert i8.reservation_count("op-pos") == 1
        assert rc01.get_state("op-pos") == "RESERVED"
        receipt = g4a.consume_and_dispatch("op-pos", payload, "idem-pos")
        assert receipt["effect_status"] == "EXECUTED"
        assert provider.execute_count("op-pos") == 1
        assert rc01.get_state("op-pos") == "DISPATCHING"
        print("positive sequence: PASS")

def test_historical_still_refused():
    gate = NativeAuthorityGate()
    gate.issue("op-old", "eval1", "h")
    ok, reason = gate.authorize_dispatch("op-old", "h")
    assert not ok and reason == "execution_not_authorized"
    print("historical path still refused: PASS")

def test_hash_mismatch_on_positive():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        provider = ProofProvider(db_path=os.path.join(d, "p.db"))
        g4a = PositiveG4A(i8, rc01, provider)
        g4a.admit_and_reserve("op1", "eval1", {"amount": 100})
        try:
            g4a.consume_and_dispatch("op1", {"amount": 200}, "idem1")
            raise AssertionError("should have refused hash mismatch")
        except RuntimeError as e:
            assert "mismatch" in str(e)
        assert provider.execute_count("op1") == 0
        print("hash mismatch on positive path: PASS")

if __name__ == "__main__":
    test_positive_sequence()
    test_historical_still_refused()
    test_hash_mismatch_on_positive()
    print("Positive contract tests: PASS")
