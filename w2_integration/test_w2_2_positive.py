"""W2 positive invariants migrated to native claims, not a legacy authorization bypass."""
from contextlib import ExitStack
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile

import pytest

from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from native_positive_g4a import NativePositiveG4A
from proof_provider import ProofProvider
from w2_2_positive_g4a import PositiveG4A
from w2_2a_authority import NativeAuthorityGate
from w2_regression_support import RESOURCE, command, event_count, native_lab, reservation


def _admit(lab, op, payload):
    return lab.auth.admit_reserve_issue(op, "eval", payload, resource_id=RESOURCE)


def _dispatch(lab, op, payload, key=None):
    return lab.provider.dispatch_authorized(lab.auth, op, key or "idem-" + op, payload)


def test_positive_sequence():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        payload = command()
        gate = NativePositiveG4A(lab.auth)
        grant = gate.evaluate_and_grant("op-pos", "eval1", payload, resource_id=RESOURCE)
        assert grant["signature"]
        assert grant["historical_execution_authorized"] is False
        assert reservation(lab, "op-pos")["state"] == "RESERVED"
        result = gate.dispatch_to_b3("op-pos", "idem-pos", payload, grant, lab.provider)
        assert result["receipt"]["effect_status"] == "EXECUTED"
        assert lab.provider.verify_receipt(result["receipt"])
        assert lab.provider.execute_count("op-pos") == 1


def test_historical_still_refused():
    gate = NativeAuthorityGate()
    gate.issue("op-old", "eval1", "h")
    ok, reason = gate.authorize_dispatch("op-old", "h")
    assert not ok and reason == "execution_not_authorized"


def test_hash_mismatch_on_positive():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        _admit(lab, "op1", command(100))
        with pytest.raises(RuntimeError, match="no valid unconsumed permit"):
            _dispatch(lab, "op1", command(200))
        assert lab.provider.execute_count("op1") == 0
        assert event_count(lab, "op1", "dispatch_claimed") == 0


def test_expired_permit_refused():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        payload = command()
        _admit(lab, "op-exp", payload)
        with lab.backend._connection() as db:
            db.execute("UPDATE w2_positive_v1_permits SET expires_at='2020-01-01T00:00:00+00:00' WHERE operation_key='op-exp'")
        with pytest.raises(RuntimeError, match="permit expired"):
            _dispatch(lab, "op-exp", payload)
        assert lab.provider.execute_count("op-exp") == 0
        with lab.backend._connection() as db:
            row = db.execute("SELECT consumed, nonce_status FROM w2_positive_v1_permits WHERE operation_key='op-exp'").fetchone()
            assert row["consumed"] == 0 and row["nonce_status"] == "ISSUED"


def test_contradictory_reissue_refused():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        original = _admit(lab, "op-re", command())
        with pytest.raises(RuntimeError, match="contradictory"):
            _admit(lab, "op-re", command(999))
        assert reservation(lab, "op-re")["reservation_id"] == original["reservation_id"]
        assert event_count(lab, "op-re", "permit_issued_after_reservation") == 1


def test_key_survives_restart_and_dispatch():
    with tempfile.TemporaryDirectory() as d:
        with native_lab(d) as lab:
            gate = NativePositiveG4A(lab.auth)
            payload = command(50)
            grant = gate.evaluate_and_grant("op-k", "eval", payload, resource_id=RESOURCE)
            key_id = lab.auth.key_id
        with native_lab(d) as lab:
            assert lab.auth.key_id == key_id
            assert lab.auth.key_custody == "EXTERNAL_HEX"
            result = NativePositiveG4A(lab.auth).dispatch_to_b3("op-k", "idem-k", payload, grant, lab.provider)
            assert result["receipt"]["effect_status"] == "EXECUTED"
            assert lab.provider.execute_count("op-k") == 1
            with pytest.raises(RuntimeError, match="already consumed"):
                NativePositiveG4A(lab.auth).dispatch_to_b3("op-k", "idem-new", payload, grant, lab.provider)
            assert lab.provider.execute_count("op-k") == 1


def test_orphan_reservation_held_not_abandoned():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        reserved = lab.backend.reserve("op-or", 100, RESOURCE)
        assert reserved.status == "RESERVED"
        with pytest.raises(RuntimeError, match="historical I8 reservation"):
            _admit(lab, "op-or", command())
        assert reservation(lab, "op-or")["state"] == "RESERVED"
        with lab.backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM reservations WHERE idempotency_key='op-or'").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_permits WHERE operation_key='op-or'").fetchone()[0] == 0
        assert lab.provider.execute_count("op-or") == 0


def test_crash_after_claim_repairs_once():
    with tempfile.TemporaryDirectory() as d:
        payload = command(40)
        with native_lab(d) as lab:
            _admit(lab, "op-c", payload)
            claim = lab.auth.consume_and_claim("op-c", payload, "idem-op-c")
            assert claim["status"] == "PENDING"
            assert lab.provider.execute_count("op-c") == 0
        with native_lab(d) as lab:
            assert lab.provider.execute_count("op-c") == 0
            result = _dispatch(lab, "op-c", payload)
            assert result["status"] == "CONFIRMED"
            assert result["claim"]["execution_id"] == claim["execution_id"]
            assert result["claim"]["claim_token"] == claim["claim_token"]
        with native_lab(d) as lab:
            result2 = _dispatch(lab, "op-c", payload)
            assert result2["receipt"] == result["receipt"]
            assert lab.provider.execute_count("op-c") == 1
            assert event_count(lab, "op-c", "dispatch_send_granted") == 1
            assert event_count(lab, "op-c", "dispatch_confirmed") == 1


def test_altered_claim_does_not_dispatch():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        payload = command(40)
        _admit(lab, "op-bad", payload)
        with lab.backend._connection() as db:
            db.execute("UPDATE w2_positive_v1_permits SET signature=? WHERE operation_key='op-bad'", (b"altered",))
        with pytest.raises(RuntimeError, match="permit signature invalid"):
            _dispatch(lab, "op-bad", payload)
        assert lab.provider.execute_count("op-bad") == 0
        assert reservation(lab, "op-bad")["state"] == "RESERVED"


def test_claim_without_reservation_does_not_dispatch():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        payload = command(40)
        _admit(lab, "op-nr", payload)
        with lab.backend._connection() as db:
            db.execute("UPDATE w2_positive_v1_capacity SET reserved=0 WHERE operation_key='op-nr'")
        with pytest.raises(RuntimeError, match="no native reservation"):
            _dispatch(lab, "op-nr", payload)
        assert lab.provider.execute_count("op-nr") == 0
        assert event_count(lab, "op-nr", "dispatch_claimed") == 0


def test_expired_consumed_claim_still_replays():
    # An existing effect may be reconciled after expiry; expiry must never
    # authorize a first send. The separate pending-claim test below covers that.
    with tempfile.TemporaryDirectory() as d:
        payload = command(40)
        with native_lab(d) as lab:
            _admit(lab, "op-ex", payload)
            original = _dispatch(lab, "op-ex", payload)
            with lab.backend._connection() as db:
                db.execute("UPDATE w2_positive_v1_permits SET expires_at='2020-01-01T00:00:00+00:00' WHERE operation_key='op-ex'")
        with native_lab(d) as lab:
            replay = _dispatch(lab, "op-ex", payload)
            assert replay["status"] == "CONFIRMED"
            assert replay["receipt"] == original["receipt"]
            assert replay["claim"]["claim_token"] == original["claim"]["claim_token"]
            assert lab.provider.execute_count("op-ex") == 1
            assert event_count(lab, "op-ex", "dispatch_send_granted") == 1


def test_expired_pending_claim_cannot_first_send():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        payload = command(40)
        _admit(lab, "op-exp-pending", payload)
        claim = lab.auth.consume_and_claim("op-exp-pending", payload, "idem-op-exp-pending")
        with lab.backend._connection() as db:
            db.execute("UPDATE w2_positive_v1_permits SET expires_at='2020-01-01T00:00:00+00:00' WHERE operation_key='op-exp-pending'")
        result = _dispatch(lab, "op-exp-pending", payload)
        assert result["status"] == "HOLD" and result["reason"] == "send right expired"
        assert result["claim"]["claim_token"] == claim["claim_token"]
        assert lab.provider.execute_count("op-exp-pending") == 0
        assert reservation(lab, "op-exp-pending")["state"] == "RESERVED"


def test_intent_completed_after_restart():
    # Native admission is atomic: crash after reservation+permit commit, before
    # the separate positive grant. Recovery must reuse the same reservation.
    with tempfile.TemporaryDirectory() as d:
        payload = command(70)
        with native_lab(d) as lab:
            issued = _admit(lab, "op-int", payload)
        with native_lab(d) as lab:
            gate = NativePositiveG4A(lab.auth)
            grant = gate.evaluate_and_grant("op-int", "eval", payload, resource_id=RESOURCE)
            assert grant["reservation_id"] == issued["reservation_id"]
            assert grant["permit_id"] == issued["permit_id"]
            assert event_count(lab, "op-int", "permit_issued_after_reservation") == 1
            result = gate.dispatch_to_b3("op-int", "idem-int", payload, grant, lab.provider)
            assert result["receipt"]["effect_status"] == "EXECUTED"
            assert lab.provider.execute_count("op-int") == 1


def test_key_id_mismatch_refused():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        payload = command(40)
        _admit(lab, "op-kid", payload)
        with lab.backend._connection() as db:
            db.execute("UPDATE w2_positive_v1_permits SET key_id=? WHERE operation_key='op-kid'", ("0" * 64,))
        with pytest.raises(RuntimeError, match="key_id mismatch"):
            _dispatch(lab, "op-kid", payload)
        assert lab.provider.execute_count("op-kid") == 0
        assert reservation(lab, "op-kid")["state"] == "RESERVED"


def _claim_and_die(path, method):
    with native_lab(path) as lab:
        claim = lab.auth.consume_and_claim("op-kill", command(40), "idem-op-kill")
        Path(path, "claim-checkpoint").write_text(claim["execution_id"], encoding="utf-8")
        if method == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(86)


@pytest.mark.parametrize("method", ["os_exit", pytest.param("sigkill", marks=pytest.mark.skipif(os.name != "posix", reason="SIGKILL requires POSIX"))])
def test_sigkill_after_claim_recovers_once(method):
    with tempfile.TemporaryDirectory() as d:
        with native_lab(d) as lab:
            _admit(lab, "op-kill", command(40))
        worker = multiprocessing.get_context("spawn").Process(target=_claim_and_die, args=(d, method))
        worker.start()
        try:
            worker.join(30)
            assert not worker.is_alive()
            assert worker.exitcode == (-signal.SIGKILL if method == "sigkill" else 86)
            execution_id = Path(d, "claim-checkpoint").read_text(encoding="utf-8")
            with native_lab(d) as lab:
                assert lab.provider.execute_count("op-kill") == 0
                first = _dispatch(lab, "op-kill", command(40))
                assert first["status"] == "CONFIRMED"
                assert first["claim"]["execution_id"] == execution_id
            with native_lab(d) as lab:
                replay = _dispatch(lab, "op-kill", command(40))
                assert replay["receipt"] == first["receipt"]
                assert lab.provider.execute_count("op-kill") == 1
                assert event_count(lab, "op-kill", "dispatch_send_granted") == 1
        finally:
            if worker.is_alive():
                worker.terminate()
            worker.join(10)


def _consume_worker(path, key, ready, start, results):
    with native_lab(path) as lab:
        ready.put(key)
        if not start.wait(30):
            raise RuntimeError("consume race start not observed")
        try:
            result = _dispatch(lab, "op-race", command(40), key)
            results.put(("ok", result["status"]))
        except RuntimeError as exc:
            results.put(("refused", str(exc)))


def test_concurrent_consume_one_effect():
    with tempfile.TemporaryDirectory() as d:
        with native_lab(d) as lab:
            _admit(lab, "op-race", command(40))
        ctx = multiprocessing.get_context("spawn")
        ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=_consume_worker, args=(d, key, ready, start, results)) for key in ("idem-a", "idem-b")]
        for worker in workers:
            worker.start()
        try:
            assert sorted([ready.get(timeout=30), ready.get(timeout=30)]) == ["idem-a", "idem-b"]
            start.set()
            outcomes = [results.get(timeout=30), results.get(timeout=30)]
            for worker in workers:
                worker.join(10)
                assert worker.exitcode == 0
            assert outcomes.count(("ok", "CONFIRMED")) == 1
            assert outcomes.count(("refused", "idempotency key conflict")) == 1
            with native_lab(d) as lab:
                assert lab.provider.execute_count("op-race") == 1
                assert event_count(lab, "op-race", "dispatch_claimed") == 1
                assert event_count(lab, "op-race", "dispatch_send_granted") == 1
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(10)
            ready.close()
            results.close()


def test_legacy_positive_dispatch_and_restart_cannot_execute_without_native_claim():
    with tempfile.TemporaryDirectory() as d, ExitStack() as cleanup:
        i8 = I8Ledger(os.path.join(d, "i8.db"))
        rc01 = RC01Lab(os.path.join(d, "rc01.db"))
        provider = ProofProvider(os.path.join(d, "provider.db"))
        for obj in (i8, rc01, provider):
            cleanup.callback(obj.conn.close)
        gate = PositiveG4A(i8, rc01, provider, os.path.join(d, "legacy-permits.db"))
        cleanup.callback(gate.conn.close)
        gate.admit_and_reserve("op-legacy", "eval", {"amount": 40})
        with pytest.raises(RuntimeError, match="authenticated kernel claim required"):
            gate.consume_and_dispatch("op-legacy", {"amount": 40}, "idem-legacy")
        assert provider.execute_count("op-legacy") == 0
        assert i8.reservation_count("op-legacy") == 1
        assert i8.net_settlement_count("op-legacy") == 0
        # Constructor recovery of the legacy persisted claim must also refuse.
        gate.conn.execute("UPDATE permits SET consumed=1, dispatch_claim=1, idempotency_key=?, payload_json=? WHERE operation_id=?",
                          ("idem-legacy", '{"amount":40}', "op-legacy"))
        gate.conn.commit()
        recovered = PositiveG4A.__new__(PositiveG4A)
        try:
            with pytest.raises(RuntimeError, match="authenticated kernel claim required"):
                recovered.__init__(i8, rc01, provider, os.path.join(d, "legacy-permits.db"))
        finally:
            if hasattr(recovered, "conn"):
                recovered.conn.close()
        assert provider.execute_count("op-legacy") == 0
