"""W2.2b: authenticated cross-boundary hashes and retained I8 during REPAIR."""
from contextlib import closing
import os
import tempfile

import pytest

from i8_ledger import I8Ledger
from w2_2b_canonical import request_hash
from w2_regression_support import RESOURCE, command, native_lab


def test_cross_boundary_hash_equality():
    payload = command(100, "a")
    evaluated_hash = request_hash(payload)
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        issued = lab.auth.admit_reserve_issue("op1", "eval1", payload, resource_id=RESOURCE)
        result = lab.provider.dispatch_authorized(lab.auth, "op1", "idem1", payload)
        assert result["status"] == "CONFIRMED"
        assert lab.provider.verify_receipt(result["receipt"])
        assert issued["request_hash"] == evaluated_hash
        assert result["receipt"]["request_hash"] == evaluated_hash
        with lab.backend._connection() as db:
            row = db.execute("SELECT request_hash, claim_token FROM w2_positive_v1_outbox WHERE operation_key='op1'").fetchone()
            assert row["request_hash"] == evaluated_hash
            assert row["claim_token"] == result["claim"]["claim_token"]


def test_divergent_hash_refused():
    with tempfile.TemporaryDirectory() as d, native_lab(d) as lab:
        first, second = command(100), command(200)
        lab.auth.admit_reserve_issue("op1", "eval1", first, resource_id=RESOURCE)
        lab.auth.admit_reserve_issue("op2", "eval2", second, resource_id=RESOURCE)
        result = lab.provider.dispatch_authorized(lab.auth, "op1", "same-key", first)
        assert result["status"] == "CONFIRMED"
        with pytest.raises(RuntimeError, match="idempotency key conflict"):
            lab.provider.dispatch_authorized(lab.auth, "op2", "same-key", second)
        assert lab.provider.execute_count("op1") == 1
        assert lab.provider.execute_count("op2") == 0
        assert lab.provider.reconcile("op1") == result["receipt"]


def test_no_release_on_repair():
    with tempfile.TemporaryDirectory() as d:
        i8 = I8Ledger(db_path=os.path.join(d, "i8.db"))
        with closing(i8.conn):
            i8.reserve("op-repair", 100)
            i8.fail_next_settle()
            with pytest.raises(RuntimeError):
                i8.settle("op-repair")
            with pytest.raises(PermissionError, match="REPAIR|release refused"):
                i8.release("op-repair")
            assert i8.net_settlement_count("op-repair") == 0
            assert i8.reservation_count("op-repair") == 1
