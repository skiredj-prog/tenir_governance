"""AC-04: positive permit lives in native consensus.sqlite3."""
import hashlib
import os
import sqlite3
import tempfile
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)
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


def test_private_key_not_stored_and_old_store_refused():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        auth = NativePositivePermitAuthority(backend)
        assert auth.key_custody == "LAB_EPHEMERAL_MEMORY"
        assert auth.productive is False
        with backend._connection() as db:
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='w2_positive_v1_keys'"
            ).fetchone() is None
        with backend._connection() as db:
            db.execute("CREATE TABLE w2_positive_v1_keys (id INTEGER)")
        try:
            NativePositivePermitAuthority(backend)
            raise AssertionError("old key table should be refused")
        except RuntimeError as exc:
            assert "w2_positive_v1_keys" in str(exc)
        print("private key not stored: PASS")


def test_external_key_hex_not_written():
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = raw.hex()
    try:
        with tempfile.TemporaryDirectory() as d:
            auth = NativePositivePermitAuthority(SQLiteConsensusBackend(d))
            assert auth.key_custody == "EXTERNAL_HEX"
            assert auth.productive is False
            pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            assert auth.key_id == hashlib.sha256(pub).hexdigest()
            with auth.backend._connection() as db:
                blob = db.execute("SELECT sql FROM sqlite_master").fetchall()
                assert not any("private_key" in (row[0] or "") for row in blob)
        print("external key not written: PASS")
    finally:
        os.environ.pop("TENIR_W2_PRIVATE_KEY_HEX", None)


def test_kernel_claim_required():
    from w2_integration.kernel_claim_provider import KernelClaimProofProvider
    with tempfile.TemporaryDirectory() as d:
        provider = KernelClaimProofProvider(db_path=d + "/p.db", audience="kernel-dispatch")
        payload = {"amount": 1}
        try:
            provider.execute("op", "idem", payload)
            raise AssertionError("missing claim should refuse")
        except RuntimeError as exc:
            assert "claim required" in str(exc)
        assert provider.execute_count("op") == 0
        claim = {
            "operation": "op",
            "idempotency_key": "idem",
            "audience": "kernel-dispatch",
            "token": "tok-1",
            "nonce": "n-1",
        }
        body = provider.execute("op", "idem", payload, claim=claim)
        assert body["effect_status"] == "EXECUTED"
        for operation_id, idem, bad, reason in (
            ("op", "idem", {**claim, "operation": "other", "nonce": "n-2"}, "operation"),
            ("op", "idem", {**claim, "idempotency_key": "other", "nonce": "n-3"}, "idempotency"),
            ("op", "idem", {**claim, "audience": "other", "nonce": "n-4"}, "audience"),
            ("op", "idem", {**claim, "token": "", "nonce": "n-5"}, "token"),
            ("op", "idem", claim, "replay"),
        ):
            try:
                provider.execute(operation_id, idem, payload, claim=bad)
                raise AssertionError(reason)
            except RuntimeError as exc:
                assert reason in str(exc)
        assert provider.execute_count("op") == 1
        print("kernel claim required: PASS")


if __name__ == "__main__":
    test_same_native_database()
    test_historical_receipt_unchanged()
    test_hash_mismatch_refused()
    test_refuses_if_historical_reservation_exists()
    test_rollback_capacity_without_permit()
    test_historical_reserve_blocked_after_positive()
    test_private_key_not_stored_and_old_store_refused()
    test_external_key_hex_not_written()
    test_kernel_claim_required()
    print("native positive tests: PASS")
