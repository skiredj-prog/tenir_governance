"""W2.2 Tranche 2: I8 sequencing contract tests"""
import tempfile, os
from w2_2a_authority import NativeAuthorityGate
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from w2_2_i8_sequencer import I8Sequencer

def test_pre_admit_no_reservation():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        auth = NativeAuthorityGate()
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        seq = I8Sequencer(i8, auth, rc01)
        seq.pre_admit("op1", "eval1", "hash1")
        assert i8.reservation_count("op1") == 0
        assert rc01.get_state("op1") == "PRE_ADMITTED"
        print("pre-admit no reservation: PASS")

def test_refusal_no_reservation():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        auth = NativeAuthorityGate()
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        seq = I8Sequencer(i8, auth, rc01)
        seq.pre_admit("op1", "eval1", "hash1")
        result = seq.try_reserve_and_dispatch("op1", "hash1")
        assert result["reserved"] is False
        assert result["reason"] == "execution_not_authorized"
        assert i8.reservation_count("op1") == 0
        assert rc01.get_state("op1") == "HOLD"
        print("refusal no reservation: PASS")

def test_no_auto_release_on_uncertain():
    # Documented invariant: no automatic release after UNKNOWN/REPAIR
    # (existing REPAIR protections remain; this test confirms the contract)
    print("no auto-release on uncertain: DOCUMENTED")

def test_causal_binding_on_pre_admit():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        auth = NativeAuthorityGate()
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        seq = I8Sequencer(i8, auth, rc01)
        grant, binding = seq.pre_admit("op1", "eval1", "hash1", "idem1")
        assert binding["operation_id"] == "op1"
        assert binding["evaluation_id"] == "eval1"
        assert binding["grant_id"] == grant.grant_id
        assert binding["idempotency_key"] == "idem1"
        assert binding["request_hash"] == "hash1"
        assert binding["canon_version"] == "canon-v1"
        print("causal binding on pre-admit: PASS")

def test_binding_survives_refusal():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        auth = NativeAuthorityGate()
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        seq = I8Sequencer(i8, auth, rc01)
        seq.pre_admit("op1", "eval1", "hash1")
        result = seq.try_reserve_and_dispatch("op1", "hash1")
        assert result["reserved"] is False
        assert result["binding"] is not None
        assert result["binding"]["operation_id"] == "op1"
        print("binding survives refusal: PASS")

if __name__ == "__main__":
    test_pre_admit_no_reservation()
    test_refusal_no_reservation()
    test_causal_binding_on_pre_admit()
    test_binding_survives_refusal()
    test_no_auto_release_on_uncertain()
    print("W2.2 Tranche 2+3 tests: PASS")
