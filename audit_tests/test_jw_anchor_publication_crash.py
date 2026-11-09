"""JW-10 exit criterion #2 — crash-safe anchor publication, CHARACTERISED.

The publication protocol spans three distinct commit points: the Kernel
commit, the Witness commit, and the third party's acceptance. This module
characterises the protocol's outcomes (APPLIED / IDEMPOTENT / HOLD /
UNKNOWN_EXTERNAL) and the idempotent, extend-only recovery after a crash before
third-party acceptance. It does NOT claim cross-domain atomicity: the three
commits remain separate and a crash can land between any two of them.

It also asserts the fix for the republication-laundering hole: the authority's
monotonicity is bound to the Witness's own append-only chain, not to a caller
counter, so a higher caller label cannot launder a jointly-restored older state
into the current anchor.
"""
from __future__ import annotations

import sqlite3

import pytest

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_integration.cold_restart_anchor_publisher import (
    AnchorDoesNotExtend,
    AnchorEquivocation,
    ExternalAnchorHold,
    MonotoneAnchorAuthority,
    PublicationOutcome,
    publish_cold_restart_anchor,
)

SUBJECT = "lab:cold-restart"
PAYLOAD = {
    "schema": "tenir-dispatch-command/v1",
    "resource_id": "lab:budget", "amount": 3, "action_type": "payment",
    "agent_id": "lab-agent", "metadata": {"recipient": "lab-no-effect"},
}


def _keys(monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())


def _authority(tmp_path, monkeypatch):
    _keys(monkeypatch)
    backend = SQLiteConsensusBackend(str(tmp_path / "kernel"))
    assert backend.register_resource("lab:budget", 60) == "REGISTERED"
    return backend, NativePositivePermitAuthority(backend, verifiable_recovery=True)


def _snap(src, dst):
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)


def _advance(authority, op):
    """One admit+claim: mints exactly one Witness checkpoint (revision += 1)."""
    authority.admit_reserve_issue(op, "eval", PAYLOAD, resource_id="lab:budget")
    authority.consume_and_claim(op, PAYLOAD, f"{op}-idem")


def _publish(authority, publisher, **kw):
    return publish_cold_restart_anchor(authority, publisher, subject=SUBJECT, **kw)


def test_publication_applies_then_is_idempotent_on_unchanged_state(tmp_path, monkeypatch):
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    assert _publish(authority, publisher).outcome is PublicationOutcome.APPLIED  # bootstrap
    _advance(authority, "op1")
    first = _publish(authority, publisher)
    assert first.outcome is PublicationOutcome.APPLIED and first.generation == 1
    again = _publish(authority, publisher)
    assert again.outcome is PublicationOutcome.IDEMPOTENT
    assert again.kernel_sha256 == first.kernel_sha256


def test_rollback_laundering_via_republication_is_refused(tmp_path, monkeypatch):
    """RED on head 8cc4290 (returned APPLIED), GREEN here: a joint restore then
    a republication with a higher caller intent is HOLD, because the generation
    is the Witness head revision, not a caller counter."""
    backend, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _publish(authority, publisher)                       # bootstrap, revision 0
    _advance(authority, "op1")
    at_rev1_k = tmp_path / "rev1_k"; at_rev1_w = tmp_path / "rev1_w"
    _snap(backend._database_path, at_rev1_k)
    _snap(authority.witness.db_path, at_rev1_w)
    assert _publish(authority, publisher).generation == 1
    _advance(authority, "op2")
    assert _publish(authority, publisher).generation == 2  # anchored at revision 2

    # Joint restore BOTH stores to the revision-1 state and republish.
    _snap(at_rev1_k, backend._database_path)
    _snap(at_rev1_w, authority.witness.db_path)
    with pytest.raises(ExternalAnchorHold, match="anti-downgrade"):
        _publish(authority, publisher)


def test_forked_higher_revision_that_does_not_extend_is_refused(tmp_path, monkeypatch):
    backend, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _publish(authority, publisher)
    _advance(authority, "op1")
    at_rev1_k = tmp_path / "rev1_k"; at_rev1_w = tmp_path / "rev1_w"
    _snap(backend._database_path, at_rev1_k)
    _snap(authority.witness.db_path, at_rev1_w)
    _publish(authority, publisher)
    _advance(authority, "op2")
    assert _publish(authority, publisher).generation == 2

    # Restore to revision 1, then grow a DIFFERENT chain to revision 3.
    _snap(at_rev1_k, backend._database_path)
    _snap(at_rev1_w, authority.witness.db_path)
    forked = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    _advance(forked, "fork-a")
    _advance(forked, "fork-b")  # revision now 3, but chain diverged at revision 2
    with pytest.raises(AnchorDoesNotExtend, match="does not extend"):
        _publish(forked, publisher)


def test_equivocation_at_same_revision_is_refused(tmp_path, monkeypatch):
    backend, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _publish(authority, publisher)
    at_rev0_k = tmp_path / "rev0_k"; at_rev0_w = tmp_path / "rev0_w"
    _snap(backend._database_path, at_rev0_k)
    _snap(authority.witness.db_path, at_rev0_w)
    _advance(authority, "op1")
    assert _publish(authority, publisher).generation == 1

    # Restore to revision 0 and mint a DIFFERENT revision-1 checkpoint.
    _snap(at_rev0_k, backend._database_path)
    _snap(at_rev0_w, authority.witness.db_path)
    other = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    _advance(other, "op-other")
    with pytest.raises(AnchorEquivocation, match="equivocation"):
        _publish(other, publisher)


def test_kernel_only_progress_within_same_witness_epoch_is_applied(tmp_path, monkeypatch):
    """An admit (no claim) advances the Kernel at the same Witness revision."""
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _advance(authority, "op1")
    first = _publish(authority, publisher)
    assert first.generation == 1
    # Kernel-only change: a second admit mints no checkpoint.
    authority.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id="lab:budget")
    progressed = _publish(authority, publisher)
    assert progressed.outcome is PublicationOutcome.APPLIED
    assert progressed.generation == 1  # same Witness epoch
    assert progressed.kernel_sha256 != first.kernel_sha256


def test_kernel_only_rollback_within_same_epoch_is_refused(tmp_path, monkeypatch):
    """Finding 3: a Kernel-only restore at an unchanged Witness revision must
    HOLD, not launder through the same-revision branch as fresh progress."""
    backend, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _advance(authority, "op1")
    at_claim_k = tmp_path / "at_claim_k"
    _snap(backend._database_path, at_claim_k)      # Kernel after claim op1
    assert _publish(authority, publisher).generation == 1
    # Kernel-only progress within the same Witness epoch (admit, no claim).
    authority.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id="lab:budget")
    assert _publish(authority, publisher).outcome is PublicationOutcome.APPLIED

    # Restore ONLY the Kernel to its post-claim prefix; Witness untouched.
    _snap(at_claim_k, backend._database_path)
    with pytest.raises(ExternalAnchorHold, match="Kernel anti-downgrade"):
        _publish(authority, publisher)


def test_kernel_fork_at_same_revision_does_not_extend(tmp_path, monkeypatch):
    backend, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _advance(authority, "op1")
    at_claim_k = tmp_path / "at_claim_k"
    _snap(backend._database_path, at_claim_k)
    # Anchor two admits of kernel progress (prefix P + {op2 events}).
    authority.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id="lab:budget")
    authority.admit_reserve_issue("op3", "eval", PAYLOAD, resource_id="lab:budget")
    assert _publish(authority, publisher).outcome is PublicationOutcome.APPLIED
    # Restore to the post-claim prefix and grow a DIFFERENT kernel tail of the
    # same length (op-x, op-y) so the event count matches but the prefix forks.
    _snap(at_claim_k, backend._database_path)
    forked = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    forked.admit_reserve_issue("op-x", "eval", PAYLOAD, resource_id="lab:budget")
    forked.admit_reserve_issue("op-y", "eval", PAYLOAD, resource_id="lab:budget")
    with pytest.raises(ExternalAnchorHold, match="anti-downgrade|does not extend"):
        _publish(forked, publisher)


def test_crash_before_acceptance_is_unknown_then_recovers_by_extension(tmp_path, monkeypatch):
    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _advance(authority, "op1")
    _publish(authority, publisher)

    # Local Kernel+Witness commits land, but the process dies before the third
    # party acknowledges the new checkpoint.
    _advance(authority, "op2")
    crashed = _publish(authority, publisher, _crash_before_acceptance=True)
    assert crashed.outcome is PublicationOutcome.UNKNOWN_EXTERNAL
    assert publisher.read_current(SUBJECT)["payload"]["generation"] == 1

    recovered = _publish(authority, publisher)
    assert recovered.outcome is PublicationOutcome.APPLIED
    assert publisher.read_current(SUBJECT)["payload"]["generation"] == 2
    assert _publish(authority, publisher).outcome is PublicationOutcome.IDEMPOTENT


def test_downgrade_during_recovery_still_holds(tmp_path, monkeypatch):
    backend, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    _advance(authority, "op1")
    at_rev1_k = tmp_path / "rev1_k"; at_rev1_w = tmp_path / "rev1_w"
    _snap(backend._database_path, at_rev1_k)
    _snap(authority.witness.db_path, at_rev1_w)
    _publish(authority, publisher)
    _advance(authority, "op2")
    _publish(authority, publisher)
    # A rollback disguised as recovery: restore to revision 1 and re-publish.
    _snap(at_rev1_k, backend._database_path)
    _snap(at_rev1_w, authority.witness.db_path)
    with pytest.raises(ExternalAnchorHold, match="anti-downgrade"):
        _publish(authority, publisher)


def test_independent_anchor_key_is_distinct_from_kernel_and_witness(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    _, authority = _authority(tmp_path, monkeypatch)
    publisher = MonotoneAnchorAuthority()
    anchor_pub = publisher.public_key_bytes
    kernel_pub = authority.private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    witness_pub = authority.witness.private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    assert len(anchor_pub) == 32
    assert anchor_pub != kernel_pub
    assert anchor_pub != witness_pub
