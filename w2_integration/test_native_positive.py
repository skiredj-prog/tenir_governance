"""AC-04: positive permit lives in native consensus.sqlite3."""
import os
import tempfile
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.experimental_g4a_settlement import G4AReceipt
from w2_integration.native_positive_permit import NativePositivePermitAuthority, PERMIT_SCHEMA


def test_same_native_database():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        auth = NativePositivePermitAuthority(backend)
        assert auth.db_path.endswith("consensus.sqlite3")
        payload = {"amount": 100}
        issued = auth.admit_reserve_issue("op-native", "eval-1", payload)
        assert issued["schema"] == PERMIT_SCHEMA
        claim = auth.consume_and_claim("op-native", payload, "idem-native")
        assert claim["claimed"] is True
        with backend._connection() as db:
            row = db.execute(
                "SELECT consumed, dispatch_claim FROM w2_positive_v1_permits WHERE operation_key='op-native'"
            ).fetchone()
            assert row["consumed"] == 1 and row["dispatch_claim"] == 1
            cap = db.execute(
                "SELECT reserved FROM w2_positive_v1_capacity WHERE operation_key='op-native'"
            ).fetchone()
            assert cap["reserved"] == 1
        print("same native database: PASS")


def test_historical_receipt_unchanged():
    receipt = G4AReceipt(
        operation_key="op", reservation_id="r", attempt_id="a", payload_hash="h",
        authority_epoch="e", trust_generation=1, operation_state="RESERVED",
        budget_state="RESERVED", version=1, event_hash="x",
    )
    assert receipt.execution_authorized is False
    assert receipt.decision == "HOLD"
    print("historical receipt unchanged: PASS")


def test_hash_mismatch_refused():
    with tempfile.TemporaryDirectory() as d:
        auth = NativePositivePermitAuthority(SQLiteConsensusBackend(d))
        auth.admit_reserve_issue("op", "eval", {"amount": 1})
        try:
            auth.consume_and_claim("op", {"amount": 2}, "idem")
            raise AssertionError("should refuse")
        except RuntimeError as exc:
            assert "no valid" in str(exc)
        print("hash mismatch refused: PASS")


if __name__ == "__main__":
    test_same_native_database()
    test_historical_receipt_unchanged()
    test_hash_mismatch_refused()
    print("native positive tests: PASS")
