"""W2.2 positive contract tests"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from w2_2_positive_g4a import PositiveG4A
from w2_2a_authority import NativeAuthorityGate
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from proof_provider import ProofProvider


def _stack(d):
    i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
    rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
    provider = ProofProvider(db_path=os.path.join(d, "p.db"))
    g4a = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
    return i8, rc01, provider, g4a


def test_positive_sequence():
    with tempfile.TemporaryDirectory() as d:
        i8, rc01, provider, g4a = _stack(d)
        payload = {"scope": {"account": "a", "amount_minor": 100, "currency": "EUR"}, "action_type": "transfer"}
        permit = g4a.admit_and_reserve("op-pos", "eval1", payload)
        assert i8.reservation_count("op-pos") == 1
        assert rc01.get_state("op-pos") == "RESERVED"
        assert permit.signature
        receipt = g4a.consume_and_dispatch("op-pos", payload, "idem-pos")
        assert receipt["effect_status"] == "EXECUTED"
        assert provider.execute_count("op-pos") == 1
        assert rc01.get_state("op-pos") == "DISPATCHING"
        # Durable consumption survives a new process on the same DB
        g4a2 = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
        try:
            g4a2.consume_and_dispatch("op-pos", payload, "idem-pos-2")
            raise AssertionError("consumed permit must not dispatch again")
        except RuntimeError as e:
            assert "consumed" in str(e)
        assert provider.execute_count("op-pos") == 1
        print("positive sequence + durable consumption: PASS")


def test_historical_still_refused():
    gate = NativeAuthorityGate()
    gate.issue("op-old", "eval1", "h")
    ok, reason = gate.authorize_dispatch("op-old", "h")
    assert not ok and reason == "execution_not_authorized"
    print("historical path still refused: PASS")


def test_hash_mismatch_on_positive():
    with tempfile.TemporaryDirectory() as d:
        _, _, provider, g4a = _stack(d)
        g4a.admit_and_reserve("op1", "eval1", {"amount": 100})
        try:
            g4a.consume_and_dispatch("op1", {"amount": 200}, "idem1")
            raise AssertionError("should have refused hash mismatch")
        except RuntimeError as e:
            assert "mismatch" in str(e)
        assert provider.execute_count("op1") == 0
        print("hash mismatch on positive path: PASS")


def test_expired_permit_refused():
    with tempfile.TemporaryDirectory() as d:
        _, _, provider, g4a = _stack(d)
        payload = {"amount": 100}
        g4a.admit_and_reserve("op-exp", "eval1", payload)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        g4a.conn.execute("UPDATE permits SET expires_at = ? WHERE operation_id = ?", (past, "op-exp"))
        g4a.conn.commit()
        try:
            g4a.consume_and_dispatch("op-exp", payload, "idem-exp")
            raise AssertionError("expired permit must be refused")
        except RuntimeError as e:
            assert "expired" in str(e)
        assert provider.execute_count("op-exp") == 0
        row = sqlite3.connect(os.path.join(d, "permits.db")).execute(
            "SELECT consumed FROM permits WHERE operation_id = ?", ("op-exp",)
        ).fetchone()
        assert row[0] == 0
        print("expired permit refused: PASS")


def test_contradictory_reissue_refused():
    with tempfile.TemporaryDirectory() as d:
        _, _, _, g4a = _stack(d)
        g4a.admit_and_reserve("op-re", "eval1", {"amount": 100})
        try:
            g4a.admit_and_reserve("op-re", "eval1", {"amount": 999})
            raise AssertionError("contradictory reissue must be refused")
        except RuntimeError as e:
            assert "contradictory" in str(e)
        print("contradictory reissue refused: PASS")


def test_key_survives_restart_and_dispatch():
    with tempfile.TemporaryDirectory() as d:
        i8, rc01, provider, g4a = _stack(d)
        payload = {"amount": 50}
        g4a.admit_and_reserve("op-k", "eval", payload)
        key_id = g4a.key_id
        g4a2 = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
        assert g4a2.key_id == key_id
        receipt = g4a2.consume_and_dispatch("op-k", payload, "idem-k")
        assert receipt["effect_status"] == "EXECUTED"
        assert provider.execute_count("op-k") == 1
        print("key survives restart: PASS")


def test_orphan_reservation_held_not_abandoned():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        rc01 = RC01Lab(db_path=os.path.join(d, "rc01.db"))
        provider = ProofProvider(db_path=os.path.join(d, "p.db"))
        i8.reserve("op-or", 100)
        rc01.transition("op-or", "RESERVED", "crash_before_permit")
        g4a = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
        assert i8.reservation_count("op-or") == 1
        hold = g4a.conn.execute(
            "SELECT reason FROM recovery_holds WHERE operation_id = ?", ("op-or",)
        ).fetchone()
        assert hold["reason"] == "reservation_without_permit_not_abandoned"
        print("orphan reservation held: PASS")


def test_crash_after_claim_repairs_once():
    with tempfile.TemporaryDirectory() as d:
        i8, rc01, provider, g4a = _stack(d)
        payload = {"amount": 40}
        g4a.admit_and_reserve("op-c", "eval", payload)
        g4a.conn.execute(
            """UPDATE permits
               SET consumed = 1, dispatch_claim = 1, idempotency_key = ?, payload_json = ?
               WHERE operation_id = 'op-c'""",
            ("idem-c", '{"amount":40}'),
        )
        g4a.conn.commit()
        assert rc01.get_state("op-c") == "RESERVED"
        assert provider.execute_count("op-c") == 0
        g4a2 = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
        assert rc01.get_state("op-c") == "DISPATCHING"
        assert provider.execute_count("op-c") == 1
        g4a3 = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
        assert provider.execute_count("op-c") == 1
        assert g4a2.key_custody == "LAB_CLEARTEXT_SQLITE"
        assert g4a3.key_id == g4a.key_id
        print("crash after claim repaired once: PASS")


def test_intent_completed_after_restart():
    with tempfile.TemporaryDirectory() as d:
        i8, rc01, provider, g4a = _stack(d)
        payload = {"amount": 70}
        from w2_2b_canonical import request_hash
        rh = request_hash(payload)
        now = "2026-10-28T00:00:00+00:00"
        g4a.conn.execute(
            """INSERT INTO permits
               (permit_id, operation_id, evaluation_id, request_hash, reservation_id,
                issued_at, expires_at, consumed, signature, key_id, status)
               VALUES ('permit-op-int', 'op-int', 'eval', ?, 'res-op-int', ?, ?, 0, NULL, ?, 'intent')""",
            (rh, now, now, g4a.key_id),
        )
        g4a.conn.commit()
        i8.reserve("op-int", 70)
        rc01.transition("op-int", "RESERVED", "crash_before_issue")
        g4a2 = PositiveG4A(i8, rc01, provider, permit_db=os.path.join(d, "permits.db"))
        row = g4a2.conn.execute("SELECT status, signature FROM permits WHERE operation_id='op-int'").fetchone()
        assert row["status"] == "issued" and row["signature"]
        receipt = g4a2.consume_and_dispatch("op-int", payload, "idem-int")
        assert receipt["effect_status"] == "EXECUTED"
        print("intent completed after restart: PASS")


if __name__ == "__main__":
    test_positive_sequence()
    test_historical_still_refused()
    test_hash_mismatch_on_positive()
    test_expired_permit_refused()
    test_contradictory_reissue_refused()
    test_key_survives_restart_and_dispatch()
    test_orphan_reservation_held_not_abandoned()
    test_intent_completed_after_restart()
    test_crash_after_claim_repairs_once()
    print("Positive contract tests: PASS")
