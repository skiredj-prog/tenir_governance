"""AC-04: positive permit lives in native consensus.sqlite3."""
import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.experimental_g4a_settlement import G4AReceipt
from w2_integration.native_positive_permit import NativePositivePermitAuthority, PERMIT_SCHEMA


def _claim_worker(db_dir, payload, gate, results):
    backend = SQLiteConsensusBackend(db_dir)
    auth = NativePositivePermitAuthority(backend)
    gate.wait(20)
    try:
        results.put(("ok", auth.consume_and_claim("op-race", payload, "idem-race")))
    except Exception as exc:
        results.put(("error", str(exc)))


def _crash_claim_worker(db_dir, payload, crash_flag, operation_key, idempotency_key):
    os.environ[crash_flag] = "1"
    backend = SQLiteConsensusBackend(db_dir)
    auth = NativePositivePermitAuthority(backend)
    auth.consume_and_claim(operation_key, payload, idempotency_key)


def _install_stable_test_keys():
    names = (
        "TENIR_W2_PRIVATE_KEY_HEX", "TENIR_WITNESS_PRIVATE_KEY_HEX",
        "TENIR_B3_PRIVATE_KEY_HEX",
    )
    prior = {name: os.environ.get(name) for name in names}
    os.environ[names[0]] = bytes(range(32)).hex()
    os.environ[names[1]] = bytes(range(32, 64)).hex()
    os.environ[names[2]] = bytes(range(64, 96)).hex()
    return prior


def _restore_test_keys(prior):
    for name, value in prior.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_same_native_database():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        auth = NativePositivePermitAuthority(backend)
        assert auth.db_path.endswith("consensus.sqlite3")
        payload = {"amount": 100}
        issued = auth.admit_reserve_issue("op-native", "eval-1", payload, resource_id="budget")
        assert issued["schema"] == PERMIT_SCHEMA
        claim = auth.consume_and_claim("op-native", payload, "idem-native")
        assert claim["claimed"] is True
        assert claim["witness"]["record"]["subject"] == "op-native:idem-native"
        assert claim["witness"]["record"]["snapshot_fingerprint"]
        assert auth.witness.verify(claim["witness"])
        with backend._connection() as db:
            row = db.execute(
                "SELECT consumed, dispatch_claim FROM w2_positive_v1_permits WHERE operation_key='op-native'"
            ).fetchone()
            assert row["consumed"] == 1 and row["dispatch_claim"] == 1
            cap = db.execute(
                "SELECT reserved, settled, reservation_id, resource_id "
                "FROM w2_positive_v1_capacity WHERE operation_key='op-native'"
            ).fetchone()
            assert cap["reserved"] == 1
            native_reservation = db.execute(
                "SELECT reservation_id, resource_id, amount, state FROM reservations "
                "WHERE idempotency_key='op-native'"
            ).fetchone()
            assert native_reservation["reservation_id"] == cap["reservation_id"]
            assert native_reservation["resource_id"] == "budget"
            assert native_reservation["state"] == "RESERVED"
            assert db.execute(
                "SELECT reserved FROM resources WHERE resource_id='budget'"
            ).fetchone()[0] == "100"
            outbox = db.execute(
                "SELECT witness_evidence_json, witness_hash FROM w2_positive_v1_outbox "
                "WHERE operation_key='op-native'"
            ).fetchone()
            assert outbox["witness_evidence_json"]
            assert outbox["witness_hash"] == claim["witness_hash"]
        print("same native database: PASS")


def test_native_i8_aggregate_capacity_prevents_overreservation():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("small-budget", 10)
        auth = NativePositivePermitAuthority(backend)
        auth.admit_reserve_issue(
            "op-budget-1", "eval-1", {"amount": 7}, resource_id="small-budget"
        )
        try:
            auth.admit_reserve_issue(
                "op-budget-2", "eval-2", {"amount": 7}, resource_id="small-budget"
            )
            raise AssertionError("aggregate I8 capacity must reject the second reservation")
        except RuntimeError as exc:
            assert "INSUFFICIENT_BUDGET" in str(exc)
        with backend._connection() as db:
            resource = db.execute(
                "SELECT capacity, reserved, committed FROM resources WHERE resource_id='small-budget'"
            ).fetchone()
            assert resource["capacity"] == "10"
            assert resource["reserved"] == "7" and resource["committed"] == "0"
            assert db.execute(
                "SELECT COUNT(*) FROM reservations WHERE resource_id='small-budget'"
            ).fetchone()[0] == 1
            assert db.execute(
                "SELECT COUNT(*) FROM w2_positive_v1_permits"
            ).fetchone()[0] == 1
            assert db.execute(
                "SELECT COUNT(*) FROM w2_positive_v1_capacity"
            ).fetchone()[0] == 1


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
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        auth = NativePositivePermitAuthority(backend)
        auth.admit_reserve_issue("op", "eval", {"amount": 1}, resource_id="budget")
        try:
            auth.consume_and_claim("op", {"amount": 2}, "idem")
            raise AssertionError("should refuse")
        except RuntimeError as exc:
            assert "no valid" in str(exc)
        print("hash mismatch refused: PASS")


def test_legacy_shadow_capacity_migrates_fail_closed():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 10)
        with backend._connection() as db:
            db.execute(
                "CREATE TABLE w2_positive_v1_capacity ("
                "operation_key TEXT PRIMARY KEY, amount INTEGER NOT NULL, "
                "reserved INTEGER NOT NULL DEFAULT 1, settled INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "INSERT INTO w2_positive_v1_capacity(operation_key, amount) VALUES ('legacy-op', 1)"
            )
        auth = NativePositivePermitAuthority(backend)
        with backend._connection() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(w2_positive_v1_capacity)")}
            assert {"reservation_id", "resource_id"}.issubset(columns)
        try:
            auth.admit_reserve_issue(
                "legacy-op", "eval", {"amount": 1}, resource_id="budget"
            )
            raise AssertionError("legacy shadow reservation must not be promoted implicitly")
        except RuntimeError:
            pass
        with backend._connection() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM w2_positive_v1_permits WHERE operation_key='legacy-op'"
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT COUNT(*) FROM reservations WHERE idempotency_key='legacy-op'"
            ).fetchone()[0] == 0


def test_refuses_if_historical_reservation_exists():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        backend.register_resource("res", 10)
        with backend._connection() as db:
            db.execute(
                """INSERT INTO reservations
                   (reservation_id, idempotency_key, resource_id, amount, state, created_at)
                   VALUES ('hist-1', 'op-hist', 'res', '1', 'RESERVED', 0)"""
            )
        auth = NativePositivePermitAuthority(backend)
        try:
            auth.admit_reserve_issue("op-hist", "eval", {"amount": 1}, resource_id="res")
            raise AssertionError("should refuse historical reservation")
        except RuntimeError as exc:
            assert "historical I8" in str(exc)
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM w2_positive_v1_capacity").fetchone()[0] == 0
        print("historical reservation blocks positive admit: PASS")


def test_rollback_capacity_without_permit():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        auth = NativePositivePermitAuthority(backend)
        os.environ["W2_FAIL_AFTER_CAPACITY"] = "1"
        try:
            auth.admit_reserve_issue("op-fail", "eval", {"amount": 1}, resource_id="budget")
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
        backend.register_resource("budget", 1000000)
        backend.register_resource("res", 10)
        auth = NativePositivePermitAuthority(backend)
        auth.admit_reserve_issue("op-own", "eval", {"amount": 1}, amount=1, resource_id="res")
        result = backend.reserve("op-own", 1, "res")
        assert result.status == "RESERVED"
        assert result.reservation_id == "res-op-own"
        with backend._connection() as db:
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
            try:
                db.execute(
                    """INSERT INTO reservations
                       (reservation_id, idempotency_key, resource_id, amount, state, created_at)
                       VALUES ('raw-1', 'op-own', 'res', '1', 'RESERVED', 0)"""
                )
                raise AssertionError("raw historical insert should abort")
            except sqlite3.IntegrityError as exc:
                assert "positive capacity owns this key" in str(exc)
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        print("historical reserve blocked after positive: PASS")


def test_private_key_not_stored_and_old_store_refused():
    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
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
        from w2_integration.proof_provider import ProofProvider
        payload = {"amount": 1}
        try:
            ProofProvider(db_path=d + "/raw.db").execute("op", "idem", payload)
            raise AssertionError("raw provider must fail closed")
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)

        backend = SQLiteConsensusBackend(d + "/native")
        backend.register_resource("budget", 1000000)
        authority = NativePositivePermitAuthority(backend)
        authority.admit_reserve_issue("op", "eval", payload, resource_id="budget")
        claim = authority.consume_and_claim("op", payload, "idem")
        provider = KernelClaimProofProvider(
            db_path=d + "/p.db", audience="kernel-dispatch",
            claim_verifier=authority.verify_dispatch_claim,
        )
        try:
            provider.execute("op", "idem", payload)
            raise AssertionError("missing claim should refuse")
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
        assert provider.execute_count("op") == 0
        altered_payload = {"amount": 2}
        assert not authority.verify_dispatch_claim("op", "idem", altered_payload, claim)
        try:
            provider.execute("op", "idem", altered_payload, claim=claim)
            raise AssertionError("claim must not authorize a different payload")
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
        assert provider.execute_count("op") == 0
        forged = {**claim, "token": "forged", "nonce": "forged", "claim_token": "forged"}
        try:
            provider.execute("op", "idem", payload, claim=forged)
            raise AssertionError("forged claim should refuse")
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
        assert provider.execute_count("op") == 0
        body = provider.execute("op", "idem", payload, claim=claim)
        assert body["effect_status"] == "EXECUTED"
        replay = provider.execute("op", "idem", payload, claim=claim)
        assert replay == body
        assert provider.execute_count("op") == 1
        provider.conn.close()
        print("kernel claim required: PASS")


def test_witness_failure_rolls_back_native_claim():
    class RejectingWitness:
        def issue(self, subject, snapshot_fingerprint):
            return {"record": {"subject": subject, "snapshot_fingerprint": snapshot_fingerprint},
                    "signature": "00"}

        def verify(self, evidence):
            return False

    with tempfile.TemporaryDirectory() as d:
        backend = SQLiteConsensusBackend(d)
        backend.register_resource("budget", 1000000)
        auth = NativePositivePermitAuthority(backend, witness=RejectingWitness())
        auth.admit_reserve_issue("op-witness-fail", "eval", {"amount": 3}, resource_id="budget")
        try:
            auth.consume_and_claim("op-witness-fail", {"amount": 3}, "idem-witness-fail")
            raise AssertionError("invalid witness must block claim")
        except RuntimeError as exc:
            assert "independent witness refused" in str(exc)
        with backend._connection() as db:
            permit = db.execute(
                "SELECT consumed, nonce_status FROM w2_positive_v1_permits "
                "WHERE operation_key='op-witness-fail'"
            ).fetchone()
            assert permit["consumed"] == 0 and permit["nonce_status"] == "ISSUED"
            assert db.execute(
                "SELECT COUNT(*) FROM w2_positive_v1_outbox "
                "WHERE operation_key='op-witness-fail'"
            ).fetchone()[0] == 0


def test_ack_lost_retries_same_claim_then_confirms_once(monkeypatch):
    from w2_integration.kernel_claim_provider import KernelClaimProofProvider

    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
        monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
        monkeypatch.setenv("TENIR_B3_PRIVATE_KEY_HEX", bytes(range(64, 96)).hex())
        payload = {"amount": 5, "currency": "EUR"}
        backend = SQLiteConsensusBackend(d + "/native")
        backend.register_resource("budget", 1000000)
        authority_ref = {}
        provider = KernelClaimProofProvider(
            db_path=d + "/provider.db",
            claim_verifier=lambda *args: authority_ref["authority"].verify_dispatch_claim(*args),
        )
        provider_public_key = provider.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        authority = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=provider_public_key
        )
        authority_ref["authority"] = authority
        authority.admit_reserve_issue("op-ack-lost", "eval-ack", payload, resource_id="budget")

        uncertain = provider.dispatch_authorized(
            authority, "op-ack-lost", "idem-ack-lost", payload, lose_ack=True
        )
        assert uncertain["status"] == "UNKNOWN"
        first_claim = uncertain["claim"]
        with backend._connection() as db:
            row = db.execute(
                "SELECT status, idempotency_key FROM w2_positive_v1_outbox "
                "WHERE operation_key='op-ack-lost'"
            ).fetchone()
            assert row["status"] == "UNKNOWN"
            assert row["idempotency_key"] == "idem-ack-lost"

        provider.conn.close()
        authority_ref["authority"] = NativePositivePermitAuthority(
            backend, trusted_provider_public_key=provider_public_key
        )
        authority = authority_ref["authority"]
        provider = KernelClaimProofProvider(
            db_path=d + "/provider.db",
            claim_verifier=lambda *args: authority_ref["authority"].verify_dispatch_claim(*args),
        )
        assert provider.key_custody == "EXTERNAL_ENV"

        forged_receipt = dict(provider.reconcile("op-ack-lost"))
        forged_receipt["effect_status"] = "FAILED"
        try:
            authority.mark_dispatch_confirmed(first_claim, forged_receipt)
            raise AssertionError("unsigned/altered receipt must not confirm dispatch")
        except RuntimeError as exc:
            assert "signature invalid" in str(exc)
        with backend._connection() as db:
            assert db.execute(
                "SELECT status FROM w2_positive_v1_outbox WHERE operation_key='op-ack-lost'"
            ).fetchone()[0] == "UNKNOWN"

        confirmed = provider.dispatch_authorized(
            authority, "op-ack-lost", "idem-ack-lost", payload
        )
        assert confirmed["status"] == "CONFIRMED"
        assert confirmed["claim"]["replayed"] is True
        assert confirmed["claim"]["execution_id"] == first_claim["execution_id"]
        assert confirmed["claim"]["claim_token"] == first_claim["claim_token"]
        assert provider.verify_receipt(confirmed["receipt"])
        assert provider.execute_count("op-ack-lost") == 1

        repeated = provider.dispatch_authorized(
            authority, "op-ack-lost", "idem-ack-lost", payload
        )
        assert repeated["status"] == "CONFIRMED" and repeated["replayed"] is True
        assert repeated["receipt"] == confirmed["receipt"]
        with backend._connection() as db:
            row = db.execute(
                "SELECT status, provider_receipt_json FROM w2_positive_v1_outbox "
                "WHERE operation_key='op-ack-lost'"
            ).fetchone()
            assert row["status"] == "CONFIRMED"
            assert json.loads(row["provider_receipt_json"]) == confirmed["receipt"]
            reservation = db.execute(
                "SELECT state FROM reservations WHERE idempotency_key='op-ack-lost'"
            ).fetchone()
            resource = db.execute(
                "SELECT reserved, committed FROM resources WHERE resource_id='budget'"
            ).fetchone()
            capacity = db.execute(
                "SELECT reserved, settled FROM w2_positive_v1_capacity "
                "WHERE operation_key='op-ack-lost'"
            ).fetchone()
            assert reservation["state"] == "COMMITTED"
            assert resource["reserved"] == "0" and resource["committed"] == "5"
            assert capacity["reserved"] == 0 and capacity["settled"] == 1
            assert db.execute(
                "SELECT COUNT(*) FROM w2_positive_v1_outbox "
                "WHERE operation_key='op-ack-lost'"
            ).fetchone()[0] == 1
        provider.conn.close()


def test_untrusted_provider_key_blocks_before_claim_consumption():
    from w2_integration.kernel_claim_provider import KernelClaimProofProvider

    with tempfile.TemporaryDirectory() as d:
        payload = {"amount": 2, "currency": "EUR"}
        backend = SQLiteConsensusBackend(d + "/native")
        backend.register_resource("budget", 1000000)
        authority = NativePositivePermitAuthority(backend)
        authority.admit_reserve_issue("op-untrusted-provider", "eval", payload, resource_id="budget")
        provider = KernelClaimProofProvider(
            db_path=d + "/provider.db",
            claim_verifier=authority.verify_dispatch_claim,
        )
        try:
            provider.dispatch_authorized(
                authority, "op-untrusted-provider", "idem-untrusted", payload
            )
            raise AssertionError("unanchored provider must not dispatch")
        except RuntimeError as exc:
            assert "provider key is not anchored" in str(exc)
        with backend._connection() as db:
            permit = db.execute(
                "SELECT consumed, nonce_status FROM w2_positive_v1_permits "
                "WHERE operation_key='op-untrusted-provider'"
            ).fetchone()
            assert permit["consumed"] == 0 and permit["nonce_status"] == "ISSUED"
            assert db.execute(
                "SELECT COUNT(*) FROM w2_positive_v1_outbox "
                "WHERE operation_key='op-untrusted-provider'"
            ).fetchone()[0] == 0
        assert provider.execute_count("op-untrusted-provider") == 0


def test_same_approval_claim_is_linearized_across_processes():
    prior = _install_stable_test_keys()
    try:
        with tempfile.TemporaryDirectory() as d:
            payload = {"amount": 7, "currency": "EUR"}
            backend = SQLiteConsensusBackend(d)
            backend.register_resource("budget", 1000000)
            authority = NativePositivePermitAuthority(backend)
            authority.admit_reserve_issue("op-race", "eval-race", payload, resource_id="budget")

            ctx = multiprocessing.get_context("spawn")
            gate = ctx.Event()
            results = ctx.Queue()
            workers = [ctx.Process(target=_claim_worker, args=(d, payload, gate, results))
                       for _ in range(2)]
            for worker in workers:
                worker.start()
            gate.set()
            outcomes = [results.get(timeout=30) for _ in workers]
            for worker in workers:
                worker.join(30)
                assert worker.exitcode == 0
            assert all(status == "ok" for status, _ in outcomes), outcomes
            claims = [body for _, body in outcomes]
            assert len({body["execution_id"] for body in claims}) == 1
            assert len({body["claim_token"] for body in claims}) == 1
            assert sum(bool(body.get("replayed")) for body in claims) == 1
            with backend._connection() as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM w2_positive_v1_outbox WHERE operation_key='op-race'"
                ).fetchone()[0] == 1
    finally:
        _restore_test_keys(prior)


def test_crash_after_commit_replays_same_witnessed_claim():
    if not hasattr(__import__("signal"), "SIGKILL"):
        pytest.skip("SIGKILL crash injection requires a POSIX platform")
    prior = _install_stable_test_keys()
    try:
        with tempfile.TemporaryDirectory() as d:
            payload = {"amount": 9, "currency": "EUR"}
            backend = SQLiteConsensusBackend(d)
            backend.register_resource("budget", 1000000)
            authority = NativePositivePermitAuthority(backend)
            authority.admit_reserve_issue("op-crash", "eval-crash", payload, resource_id="budget")

            worker = multiprocessing.get_context("spawn").Process(
                target=_crash_claim_worker,
                args=(d, payload, "W2_CRASH_AFTER_NATIVE_CLAIM", "op-crash", "idem-crash"),
            )
            worker.start()
            worker.join(30)
            assert worker.exitcode == -9

            recovered = authority.consume_and_claim("op-crash", payload, "idem-crash")
            assert recovered["replayed"] is True
            assert authority.witness.verify(recovered["witness"])
            with backend._connection() as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM w2_positive_v1_outbox WHERE operation_key='op-crash'"
                ).fetchone()[0] == 1
    finally:
        _restore_test_keys(prior)


def test_crash_before_commit_rolls_back_kernel_and_allows_retry():
    if not hasattr(__import__("signal"), "SIGKILL"):
        pytest.skip("SIGKILL crash injection requires a POSIX platform")
    prior = _install_stable_test_keys()
    try:
        with tempfile.TemporaryDirectory() as d:
            payload = {"amount": 11, "currency": "EUR"}
            backend = SQLiteConsensusBackend(d)
            backend.register_resource("budget", 1000000)
            authority = NativePositivePermitAuthority(backend)
            authority.admit_reserve_issue("op-precommit-crash", "eval-precommit", payload, resource_id="budget")

            worker = multiprocessing.get_context("spawn").Process(
                target=_crash_claim_worker,
                args=(d, payload, "W2_CRASH_BEFORE_NATIVE_CLAIM_COMMIT",
                      "op-precommit-crash", "idem-crash"),
            )
            worker.start()
            worker.join(30)
            assert worker.exitcode == -9

            with backend._connection() as db:
                permit = db.execute(
                    "SELECT consumed, nonce_status FROM w2_positive_v1_permits "
                    "WHERE operation_key='op-precommit-crash'"
                ).fetchone()
                assert permit["consumed"] == 0 and permit["nonce_status"] == "ISSUED"
                assert db.execute(
                    "SELECT COUNT(*) FROM w2_positive_v1_outbox "
                    "WHERE operation_key='op-precommit-crash'"
                ).fetchone()[0] == 0
            with sqlite3.connect(d + "/consensus.sqlite3.witness.sqlite3") as witness_db:
                assert witness_db.execute(
                    "SELECT COUNT(*) FROM witness_checkpoints"
                ).fetchone()[0] == 1

            retry = authority.consume_and_claim(
                "op-precommit-crash", payload, "idem-crash"
            )
            assert retry["claimed"] is True and not retry.get("replayed")
            assert authority.witness.verify(retry["witness"])
            with backend._connection() as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM w2_positive_v1_outbox "
                    "WHERE operation_key='op-precommit-crash'"
                ).fetchone()[0] == 1
    finally:
        _restore_test_keys(prior)


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
    test_witness_failure_rolls_back_native_claim()
    test_ack_lost_retries_same_claim_then_confirms_once()
    test_untrusted_provider_key_blocks_before_claim_consumption()
    test_same_approval_claim_is_linearized_across_processes()
    test_crash_after_commit_replays_same_witnessed_claim()
    test_crash_before_commit_rolls_back_kernel_and_allows_retry()
    print("native positive tests: PASS")
