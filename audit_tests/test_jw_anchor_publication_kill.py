"""JW-10 exit criterion #2 — REAL process-kill between the local commits and
third-party acceptance.

A child process performs a native admit + claim (Kernel commit, then Witness
commit) and is then killed with ``os._exit`` before the external anchor is
extended (via the ``W2_CRASH_BEFORE_ANCHOR_PUBLICATION`` hook inside
``consume_and_claim``). The parent, which holds the external monotone authority,
then verifies:

- the child's local commits are durable (the stores advanced);
- the external anchor did NOT advance (it stayed at the pre-crash revision);
- the next gated native operation HOLDs, because the committed state no longer
  matches the stale anchor;
- extend-only recovery republishes the advanced state and the gated operation
  then proceeds.

This exercises the three distinct commit points with a genuine kill; it does
NOT make them atomic. ``os._exit`` is used (portable) rather than SIGKILL.
"""
from __future__ import annotations

import multiprocessing as mp
import os

import pytest

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_integration.native_positive_permit import NativePositivePermitAuthority
from w2_integration.external_cold_restart_anchor import (
    ExternalColdRestartAnchorGate,
    ExternalAnchorHold,
)
from w2_integration.cold_restart_anchor_publisher import (
    MonotoneAnchorAuthority,
    PublicationOutcome,
    publish_cold_restart_anchor,
    _witness_head,
)

SUBJECT = "lab:cold-restart"
RESOURCE = "lab:budget"
PAYLOAD = {
    "schema": "tenir-dispatch-command/v1",
    "resource_id": RESOURCE, "amount": 3, "action_type": "payment",
    "agent_id": "lab-agent", "metadata": {"recipient": "lab-no-effect"},
}


def _child_commit_then_kill(kernel_dir, op, idem):
    """Run in a spawned child: commit a claim, then die before publication."""
    os.environ["W2_CRASH_BEFORE_ANCHOR_PUBLICATION"] = "1"
    backend = SQLiteConsensusBackend(kernel_dir)
    authority = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    authority.admit_reserve_issue(op, "eval", PAYLOAD, resource_id=RESOURCE)
    authority.consume_and_claim(op, PAYLOAD, idem)  # os._exit(1) fires inside
    os._exit(7)  # only reached if the crash hook failed to fire


def test_real_kill_between_commit_and_publication_holds_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("TENIR_W2_PRIVATE_KEY_HEX", bytes(range(32)).hex())
    monkeypatch.setenv("TENIR_WITNESS_PRIVATE_KEY_HEX", bytes(range(32, 64)).hex())
    kernel_dir = str(tmp_path / "kernel")
    backend = SQLiteConsensusBackend(kernel_dir)
    assert backend.register_resource(RESOURCE, 90) == "REGISTERED"
    authority = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    publisher = MonotoneAnchorAuthority()

    # Bootstrap revision 1 and anchor it (held by the parent).
    authority.admit_reserve_issue("op0", "eval", PAYLOAD, resource_id=RESOURCE)
    authority.consume_and_claim("op0", PAYLOAD, "op0-idem")
    assert publish_cold_restart_anchor(
        authority, publisher, subject=SUBJECT).generation == 1
    witness_db = authority.witness.db_path
    assert _witness_head(witness_db)[0] == 1

    # Child commits a second claim and is killed before publication.
    ctx = mp.get_context("spawn")
    child = ctx.Process(target=_child_commit_then_kill,
                        args=(kernel_dir, "op1", "op1-idem"))
    child.start()
    child.join(60)
    assert child.exitcode == 1  # died via the crash hook, not the os._exit(7) tail

    # Local commits are durable; the external anchor never advanced.
    assert _witness_head(witness_db)[0] == 2
    assert publisher.read_current(SUBJECT)["payload"]["generation"] == 1

    # The next gated op HOLDs: committed state no longer matches the anchor.
    restarted = NativePositivePermitAuthority(backend, verifiable_recovery=True)
    gate = ExternalColdRestartAnchorGate(
        reader=publisher, trusted_public_key=publisher.public_key_bytes,
        required_topology=restarted.topology_id)
    restarted.attach_cold_restart_anchor_gate(gate, subject=SUBJECT)
    with pytest.raises(ExternalAnchorHold, match="rollback or drift"):
        restarted.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id=RESOURCE)

    # Extend-only recovery republishes the advanced state; the gate then passes.
    recovered = publish_cold_restart_anchor(restarted, publisher, subject=SUBJECT)
    assert recovered.outcome is PublicationOutcome.APPLIED
    assert publisher.read_current(SUBJECT)["payload"]["generation"] == 2
    restarted.attach_cold_restart_anchor_gate(
        gate, subject=SUBJECT, publisher=publisher)
    permit = restarted.admit_reserve_issue("op2", "eval", PAYLOAD, resource_id=RESOURCE)
    assert permit["permit_id"] == "permit-op2"
