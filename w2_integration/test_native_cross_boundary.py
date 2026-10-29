"""Native positive path and B3 must share canon-v1 request_hash."""
import tempfile
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash
from proof_provider import ProofProvider
from native_positive_permit import NativePositivePermitAuthority


def test_native_b3_same_hash():
    payload = {"amount": 100, "currency": "EUR"}
    expected = request_hash(payload)
    with tempfile.TemporaryDirectory() as d:
        auth = NativePositivePermitAuthority(SQLiteConsensusBackend(d))
        issued = auth.admit_reserve_issue("op-x", "eval-x", payload)
        assert issued["request_hash"] == expected
        provider = ProofProvider(allow_unclaimed_execution=True, db_path=d + "/provider.db")
        receipt = provider.execute("op-x", "idem-x", payload)
        assert receipt["request_hash"] == expected
        assert receipt["request_hash"] == issued["request_hash"]
        print("native == B3 hash: PASS")


def test_divergent_refused_before_effect():
    with tempfile.TemporaryDirectory() as d:
        auth = NativePositivePermitAuthority(SQLiteConsensusBackend(d))
        auth.admit_reserve_issue("op-y", "eval-y", {"amount": 100})
        provider = ProofProvider(allow_unclaimed_execution=True, db_path=d + "/provider.db")
        try:
            auth.consume_and_claim("op-y", {"amount": 200}, "idem-y")
            raise AssertionError("native claim should refuse")
        except RuntimeError:
            pass
        assert provider.execute_count("op-y") == 0
        print("divergent refused before effect: PASS")


if __name__ == "__main__":
    test_native_b3_same_hash()
    test_divergent_refused_before_effect()
    print("native cross-boundary: PASS")
