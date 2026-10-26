"""P0 joint-boundary NEGATIVE CONTROLS against the ACTUAL G4-A lab SUT.

These tests intentionally expose present vulnerabilities. A PASS means the
counterexample reproduced, NOT that R-01/Q1/Q6 conform. No provider dispatch,
no remote witness, no real G1 one-time grant or production effect is exercised.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tenir_governance.operations.experimental_g4a_settlement import (
    ExperimentalG4AAuthority,
)
from tenir_governance.operations.experimental_g3_exclusive_owner import (
    G3ExperimentalExclusiveOwner,
)
from tenir_governance.operations.experimental_native_sqlite_authority import (
    SqliteAuthorityHold,
)
from tenir_governance.operations.registry import (
    DurableOperationRegistry, OperationRecord,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend


def _record(key="lab:q6"):
    return OperationRecord(
        idempotency_key=key, passage_id="lab:passage",
        agent_id="lab:agent", action_type="simulation",
        resource_id="lab:budget", amount=20.0,
    )


@pytest.fixture
def actual_g4a(tmp_path):
    source = DurableOperationRegistry(str(tmp_path / "source.json"))
    for key in ("lab:q6", "lab:q6-other"):
        assert source.register(_record(key))
    backend = SQLiteConsensusBackend(str(tmp_path / "budget"))
    backend.register_resource("lab:budget", Decimal("100"))
    G3ExperimentalExclusiveOwner(
        source, backend, experimental_only=True
    ).activate()
    authority = ExperimentalG4AAuthority.provision(
        backend, trust_generation=1, experimental_only=True
    )
    return source, backend, authority


def _admit(authority, key="lab:q6"):
    return authority.admit_created(
        _record(key), attempt_id="lab:attempt:" + key,
        request_id="lab:admit:" + key,
    )


def _reservation_count(backend):
    with backend._connection() as db:
        return db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]


def _test_only_release_verifier(*, authorized_by, scope, authorization, reason, lock_id):
    # Only used to manufacture a GENUINE previously authorized historical
    # inactive mark. The fixed test token is NOT production authentication.
    if authorization != "LAB-TEST-RELEASE" or not lock_id:
        return None
    return {
        "authorized": True, "principal": authorized_by, "scope": scope,
        "lock_id": lock_id, "authorization_id": "test-release-" + lock_id,
    }


def _restore_archived_inactive_quarantine(source):
    owner = DurableOperationRegistry(
        source._store_path,
        quarantine_release_verifier=_test_only_release_verifier,
    )
    owner.set_quarantine(True, locked_by="incident-1", incident_id="q6-1")
    release = owner.authorized_release_quarantine(
        authorized_by="lab-operator", scope="lab:q6",
        authorization="LAB-TEST-RELEASE", reason="test historical release",
    )
    assert release["released"] is True
    marker = Path(owner._quarantine_path())
    archived_valid_inactive_mark = marker.read_bytes()
    assert owner.quarantine_is_active() is False

    # Later, a DIFFERENT incident locks execution again.
    owner.set_quarantine(True, locked_by="incident-2", incident_id="q6-2")
    assert owner.quarantine_is_active() is True

    # Attacker has only same-UID access to local mark. The independent
    # witness is NOT touched because actual G4-A does not consult one.
    marker.write_bytes(archived_valid_inactive_mark)
    assert owner.quarantine_is_active() is False
    return owner


def test_q6_counterexample_actual_g4a_admits_after_old_valid_mark_restored(actual_g4a):
    """Expected vulnerability reproduction, NOT a security/conformance PASS."""
    source, backend, authority = actual_g4a
    owner = DurableOperationRegistry(
        source._store_path,
        quarantine_release_verifier=_test_only_release_verifier,
    )
    owner.set_quarantine(True, locked_by="incident-1", incident_id="q6-1")
    release = owner.authorized_release_quarantine(
        authorized_by="lab-operator", scope="lab:q6",
        authorization="LAB-TEST-RELEASE", reason="test historical release",
    )
    assert release["released"] is True
    historical = Path(owner._quarantine_path()).read_bytes()
    owner.set_quarantine(True, locked_by="incident-2", incident_id="q6-2")
    assert owner.quarantine_is_active() is True

    # Positive security control: before attack G4-A correctly refuses.
    with pytest.raises(SqliteAuthorityHold, match="quarantine"):
        _admit(authority)
    assert _reservation_count(backend) == 0

    # Q6: restore an older, structurally valid and actually authorized
    # INACTIVE local quarantine file; do not modify G4-A SQLite.
    Path(owner._quarantine_path()).write_bytes(historical)
    assert owner.quarantine_is_active() is False

    receipt = _admit(authority)
    assert receipt.operation_state == "RESERVED"
    assert receipt.execution_authorized is False
    assert _reservation_count(backend) == 1


@pytest.mark.xfail(
    strict=True,
    reason="P0 Q6 OPEN: actual G4-A does not verify independently pinned E8 quarantine head",
)
def test_q6_required_g4a_must_hold_after_restored_inactive_mark(actual_g4a):
    """Normative expectation, intentionally RED until full G1×E8×G4-A."""
    source, backend, authority = actual_g4a
    _restore_archived_inactive_quarantine(source)
    with pytest.raises(SqliteAuthorityHold, match="quarantine|E8|witness"):
        _admit(authority)
    assert _reservation_count(backend) == 0


def test_q1_counterexample_actual_g4a_admits_after_marker_and_sentinel_deleted(actual_g4a):
    """Q1 is distinct from Q6: both local continuity artifacts are deleted."""
    source, backend, authority = actual_g4a
    source.set_quarantine(True, locked_by="q1-incident")
    with pytest.raises(SqliteAuthorityHold, match="quarantine"):
        _admit(authority)
    marker = Path(source._quarantine_path())
    sentinel = Path(source._quarantine_initialized_path())
    assert marker.exists() and sentinel.exists()
    marker.unlink()
    sentinel.unlink()
    # No independent witness exists on the actual G4-A path.
    assert source.quarantine_is_active() is False
    receipt = _admit(authority)
    assert receipt.operation_state == "RESERVED"
    assert receipt.execution_authorized is False
    assert _reservation_count(backend) == 1


@pytest.mark.xfail(
    strict=True,
    reason="P0 Q1 OPEN: deleting local marker and sentinel must not reset authority",
)
def test_q1_required_g4a_must_hold_after_full_local_deletion(actual_g4a):
    source, backend, authority = actual_g4a
    source.set_quarantine(True, locked_by="q1-incident")
    Path(source._quarantine_path()).unlink()
    Path(source._quarantine_initialized_path()).unlink()
    with pytest.raises(SqliteAuthorityHold, match="quarantine|E8|witness"):
        _admit(authority)
    assert _reservation_count(backend) == 0
