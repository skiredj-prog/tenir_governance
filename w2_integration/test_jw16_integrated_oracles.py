"""JW-16: anchor uses the real local revision; orphan recovery is integrated."""
import multiprocessing
import sqlite3

import pytest

from external_anchor import ExternalMonotoneAnchor
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from test_native_positive import _crash_claim_worker, _install_stable_test_keys, _restore_test_keys


def test_anchor_uses_the_real_local_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    backend = SQLiteConsensusBackend(str(tmp_path / "kernel"))
    backend.register_resource("lab:budget", 20)
    authority = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    payload = {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": "lab:budget", "amount": 7, "action_type": "payment",
        "agent_id": "lab-agent", "metadata": {"recipient": "r"},
    }
    authority.admit_reserve_issue("op", "eval", payload, resource_id="lab:budget")
    claim = authority.consume_and_claim("op", payload, "idem")
    anchor = ExternalMonotoneAnchor(str(tmp_path / "anchor.sqlite3"))
    anchor.note("op:idem", claim["witness"]["record"]["revision"],
                claim["execution_id"], claim["claim_token"])
    with sqlite3.connect(authority.witness.db_path) as db:
        db.execute("DELETE FROM witness_checkpoints")
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    assert restarted.local_witness_revision("op:idem") == 0
    with pytest.raises(RuntimeError, match="behind external anchor"):
        restarted.check_anchor(anchor, "op:idem")


def test_integrated_crash_reuses_orphan_witness_identity():
    if not hasattr(__import__("signal"), "SIGKILL"):
        pytest.skip("SIGKILL crash injection requires a POSIX platform")
    prior = _install_stable_test_keys()
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            payload = {"amount": 11, "currency": "EUR"}
            backend = SQLiteConsensusBackend(directory)
            backend.register_resource("budget", 1000000)
            authority = NativePositivePermitAuthority(backend, verifiable_recovery=True)
            authority.admit_reserve_issue(
                "op-precommit-crash", "eval-precommit", payload, resource_id="budget",
            )
            worker = multiprocessing.get_context("spawn").Process(
                target=_crash_claim_worker,
                args=(directory, payload, "W2_CRASH_BEFORE_NATIVE_CLAIM_COMMIT",
                      "op-precommit-crash", "idem-crash"),
            )
            worker.start()
            worker.join(30)
            assert worker.exitcode == -9
            with sqlite3.connect(directory + "/consensus.sqlite3.witness.sqlite3") as db:
                orphan = db.execute(
                    "SELECT record_json FROM witness_checkpoints"
                ).fetchone()
            assert orphan is not None
            import json
            orphan_record = json.loads(orphan[0])
            retry = authority.consume_and_claim(
                "op-precommit-crash", payload, "idem-crash"
            )
            assert retry["execution_id"] == orphan_record["execution_id"]
            assert retry["claim_token"] == orphan_record["claim_token"]
            with sqlite3.connect(directory + "/consensus.sqlite3.witness.sqlite3") as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM witness_checkpoints"
                ).fetchone()[0] == 1
            with backend._connection() as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM w2_positive_v1_outbox"
                ).fetchone()[0] == 1
    finally:
        _restore_test_keys(prior)
