"""W2.2a integration: gate is mandatory on the dispatch path"""
import tempfile, os
from w2_2_integrated import run_integrated_candidate
from proof_provider import ProofProvider

def test_gate_blocks_provider_call():
    with tempfile.TemporaryDirectory() as d:
        result = run_integrated_candidate(d, "op-blocked")
        assert result["dispatch_refused"] is True
        assert result["refuse_reason"] == "execution_not_authorized"
        assert result["provider_called"] is False
        assert result["execute_count"] == 0
        # Verify no row in provider DB
        provider = ProofProvider(db_path=os.path.join(d, "provider.db"))
        assert provider.execute_count("op-blocked") == 0
        print("gate blocks provider call: PASS")

if __name__ == "__main__":
    test_gate_blocks_provider_call()
    print("W2.2a path test: PASS")
