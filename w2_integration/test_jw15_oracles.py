"""Separate oracles for external anchoring and orphan recovery.

The joint-rollback gap remains open when the anchor is rolled back too.
"""
import sqlite3

import pytest

from external_anchor import ExternalMonotoneAnchor
from independent_witness import IndependentWitness
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def _keys(monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())


def _snapshot(src, dst):
    with sqlite3.connect(str(src)) as source, sqlite3.connect(str(dst)) as dest:
        source.backup(dest)


def test_external_anchor_detects_joint_local_rollback(tmp_path, monkeypatch):
    _keys(monkeypatch)
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
    anchor.note(
        "op:idem", claim["witness"]["record"]["revision"],
        claim["execution_id"], claim["claim_token"],
    )
    prior_kernel = tmp_path / "prior-kernel.sqlite3"
    prior_witness = tmp_path / "prior-witness.sqlite3"
    _snapshot(backend._database_path, prior_kernel)
    _snapshot(authority.witness.db_path, prior_witness)
    # Roll local stores back to empty-of-claim state by restoring pre-claim copies.
    empty_kernel = tmp_path / "empty-kernel"
    empty = SQLiteConsensusBackend(str(empty_kernel))
    empty.register_resource("lab:budget", 20)
    NativePositivePermitAuthority(empty, verifiable_recovery=True)
    _snapshot(empty._database_path, backend._database_path)
    with sqlite3.connect(authority.witness.db_path) as db:
        db.execute("DELETE FROM witness_checkpoints")
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    with pytest.raises(RuntimeError, match="behind external anchor"):
        anchor.require_not_behind("op:idem", 0)
    assert restarted.verify_dispatch_claim("op", "idem", payload, claim) is False


def test_anchor_rollback_is_still_undetected(tmp_path, monkeypatch):
    """Own oracle: if the anchor is restored too, detection fails. OPEN."""
    anchor_path = tmp_path / "anchor.sqlite3"
    anchor = ExternalMonotoneAnchor(str(anchor_path))
    anchor.note("op:idem", 1, "exec-1", "token-1")
    prior = tmp_path / "anchor-prior.sqlite3"
    _snapshot(anchor_path, prior)
    anchor.note("op:idem", 2, "exec-1", "token-1")
    _snapshot(prior, anchor_path)
    restored = ExternalMonotoneAnchor(str(anchor_path))
    assert restored.latest("op:idem")["revision"] == 1


def test_orphan_witness_recovery_reuses_identity(tmp_path, monkeypatch):
    _keys(monkeypatch)
    witness = IndependentWitness(str(tmp_path / "w.sqlite"), verifiable_recovery=True)
    first = witness.issue(
        "op:idem", "ab" * 32,
        identity={"execution_id": "exec-original", "claim_token": "token-original"},
    )
    recovered = IndependentWitness(str(tmp_path / "w.sqlite"), verifiable_recovery=True)
    replay = recovered.issue(
        "op:idem", "ab" * 32,
        identity={"execution_id": "exec-NEW", "claim_token": "token-NEW"},
    )
    assert replay["record"]["execution_id"] == "exec-original"
    assert replay["record"]["claim_token"] == "token-original"
    with sqlite3.connect(str(tmp_path / "w.sqlite")) as db:
        assert db.execute("SELECT COUNT(*) FROM witness_checkpoints").fetchone()[0] == 1
