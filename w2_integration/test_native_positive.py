"""AC-04: positive permit lives in native consensus.sqlite3."""
import os
import sqlite3
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


def test_refuses_if_historical_reservation_exists():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("res", 10)
        with backend._connection() as db:
            db.execute(
                """INSERT INTO reservations
                   (reservation_id, idempotency_key, resource_id, amount, state, created_at)
                   VALUES ('hist-1', 'op-hist', 'res', '1', 'RESERVED', 0)"""
            )
        auth = NativePositivePermitAuthority(backend)
        try:
            auth.admit_reserve_issue("op-hist", "eval", {"amount": 1})
            raise AssertionError("should refuse historical reservation")
        except RuntimeError as exc:
            assert "historical I8" in str(exc)
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_capacity").fetchone()[0] == 0
        print("historical reservation blocks positive admit: PASS")


def test_rollback_capacity_without_permit():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        auth = NativePositivePermitAuthority(backend)
        os.environ["W2_FAIL_AFTER_CAPACITY"] = "1"
        try:
            auth.admit_reserve_issue("op-fail", "eval", {"amount": 1})
            raise AssertionError("injected failure should abort")
        except RuntimeError as exc:
            assert "injected failure" in str(exc)
        finally:
            os.environ.pop("W2_FAIL_AFTER_CAPACITY", None)
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_capacity").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_events").fetchone()[0] == 0
        print("rollback capacity without permit: PASS")


def test_historical_reserve_blocked_after_positive():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("res", 10)
        auth = NativePositivePermitAuthority(backend)
        auth.admit_reserve_issue("op-own", "eval", {"amount": 1})
        result = backend.reserve("op-own", 1, "res")
        assert result.status == "POSITIVE_AUTHORITY_OWNS_KEY"
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
            try:
                db.execute(
                    """INSERT INTO reservations
                       (reservation_id, idempotency_key, resource_id, amount, state, created_at)
                       VALUES ('raw-1', 'op-own', 'res', '1', 'RESERVED', 0)"""
                )
                raise AssertionError("raw historical insert should abort")
            except sqlite3.IntegrityError as exc:
                assert "positive capacity owns this key" in str(exc)
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        print("historical reserve blocked after positive: PASS")


if __name__ == "__main__":
    test_same_native_database()
    test_historical_receipt_unchanged()
    test_hash_mismatch_refused()
    test_refuses_if_historical_reservation_exists()
    test_rollback_capacity_without_permit()
    test_historical_reserve_blocked_after_positive()
    print("native positive tests: PASS")
