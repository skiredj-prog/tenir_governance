"""JW-11: positive grants require a stable injected key. Not independent custody."""
import os
import tempfile

import pytest

from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend

OPERATION = "lab:jw11"
EVALUATION = "eval-jw11"
RESOURCE = "lab:budget"
KEY = bytes(range(32)).hex()


def _command():
    return {
        "schema": "tenir-dispatch-command/v1",
        "resource_id": RESOURCE,
        "amount": 7,
        "action_type": "payment",
        "agent_id": "lab-agent",
        "metadata": {"recipient": "recipient-1"},
    }


def _open(directory, key):
    previous = os.environ.get("TENIR_W2_PRIVATE_KEY_HEX")
    os.environ["TENIR_WITNESS_PRIVATE_KEY_HEX"] = bytes(range(32, 64)).hex()
    if key is None:
        os.environ.pop("TENIR_W2_PRIVATE_KEY_HEX", None)
    else:
        os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = key
    backend = SQLiteConsensusBackend(directory)
    backend.register_resource(RESOURCE, 50)
    authority = NativePositivePermitAuthority(backend)
    return previous, NativePositiveG4A(authority)


def _restore(previous):
    if previous is None:
        os.environ.pop("TENIR_W2_PRIVATE_KEY_HEX", None)
    else:
        os.environ["TENIR_W2_PRIVATE_KEY_HEX"] = previous


def test_ephemeral_key_cannot_issue_a_positive_grant():
    previous = os.environ.get("TENIR_W2_PRIVATE_KEY_HEX")
    directory = tempfile.mkdtemp()
    try:
        _, gate = _open(directory, None)
        assert gate.authority.key_custody == "LAB_EPHEMERAL_MEMORY"
        with pytest.raises(RuntimeError, match="injected key"):
            gate.evaluate_and_grant(OPERATION, EVALUATION, _command(), resource_id=RESOURCE)
        with gate.authority._tx() as db:
            assert db.execute("SELECT COUNT(*) FROM w2_positive_g4a_v1_grants").fetchone()[0] == 0
    finally:
        _restore(previous)


def test_restart_with_a_different_key_is_refused():
    previous = os.environ.get("TENIR_W2_PRIVATE_KEY_HEX")
    directory = tempfile.mkdtemp()
    try:
        _, gate = _open(directory, KEY)
        gate.evaluate_and_grant(OPERATION, EVALUATION, _command(), resource_id=RESOURCE)
        with pytest.raises(RuntimeError, match="key changed"):
            _open(directory, bytes(range(64, 96)).hex())
    finally:
        _restore(previous)


def test_restart_with_the_same_key_keeps_the_grant():
    previous = os.environ.get("TENIR_W2_PRIVATE_KEY_HEX")
    directory = tempfile.mkdtemp()
    try:
        _, gate = _open(directory, KEY)
        grant = gate.evaluate_and_grant(OPERATION, EVALUATION, _command(), resource_id=RESOURCE)
        _, reopened = _open(directory, KEY)
        reopened._require_grant(OPERATION, _command(), grant)
        with reopened.authority._tx() as db:
            assert db.execute(
                "SELECT public_key_hex FROM w2_positive_g4a_v1_custody"
            ).fetchone()[0] == reopened.public_key_bytes().hex()
            columns = [row[1] for row in db.execute("PRAGMA table_info(w2_positive_g4a_v1_custody)")]
        assert "private_key_hex" not in columns
    finally:
        _restore(previous)
