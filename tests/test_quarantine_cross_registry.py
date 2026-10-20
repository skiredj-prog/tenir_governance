"""Single-host QUARANTINE_LOCK lifecycle and interprocess linearization.

The verifier below uses an HMAC *test fixture*, not a production credential
provider. Production defaults to refusing unlock without an injected verifier.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

from tenir_governance.composition.governor import DualKernelGovernor
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord, OperationState
from tenir_kernel.kernel import TenirKernel
from tenir_governance.composition import ScoringKernelAdapter
from tenir_governance.objects import Agent, Passage

_TEST_SIGNING_KEY = b"quarantine-test-only-nonproduction-authentication-key"


def _test_token(lock_id: str, *, principal="operator", scope="lei") -> str:
    msg = f"{lock_id}|{principal}|{scope}".encode()
    return hmac.new(_TEST_SIGNING_KEY, msg, hashlib.sha256).hexdigest()


def _trusted_test_verifier(*, authorized_by, scope, authorization, reason, lock_id):
    expected = _test_token(lock_id, principal=authorized_by, scope=scope)
    if not hmac.compare_digest(authorization, expected):
        return None
    return {
        "authorized": True,
        "principal": authorized_by,
        "scope": scope,
        "lock_id": lock_id,
        "authorization_id": "test-hmac-" + lock_id,
    }


def _governor(path, *, verifier=None):
    state = MembraneState()
    scoring = ScoringKernelAdapter(TenirKernel())
    membrane = TransitionMembrane(state)
    registry = DurableOperationRegistry(str(path), quarantine_release_verifier=verifier)
    return DualKernelGovernor(scoring, membrane, registry=registry)


def _register_reserved(governor, key="op-q"):
    record = OperationRecord(
        idempotency_key=key,
        passage_id=f"p-{key}",
        agent_id="alice",
        action_type="payment",
    )
    assert governor.registry.register(record)
    assert governor.registry.transition_to(key, OperationState.RESERVED)[0]


def _lock_id(path):
    return json.loads(Path(str(path) + ".quarantine.json").read_text())["lock_id"]


def _release(owner, path, *, token=None):
    return owner.release_quarantine(
        authorized_by="operator", scope="lei",
        authorization=token if token is not None else _test_token(_lock_id(path)),
        reason="independent incident review complete",
    )


def _child_dispatch(path, key, queue):
    """Executed under spawn: independent interpreter, state, and registry."""
    try:
        governor = _governor(path)
        result = governor.dispatch(key)
        queue.put({
            "dispatched": result["dispatched"],
            "reason": result.get("reason", ""),
            "state": governor.registry.get(key).state.value,
        })
    except BaseException as exc:
        queue.put({"error": repr(exc)})


def _child_activate_then_crash(path):
    registry = DurableOperationRegistry(str(path))
    registry.set_quarantine(True, locked_by="child-crash")
    os._exit(0)


def test_second_registry_blocks_dispatch_until_verified_release(tmp_path):
    store = tmp_path / "ops.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    _register_reserved(owner)
    # The direct membrane entry point must write the shared mark as well.
    owner.membrane.lock_quarantine(locked_by="watch")

    other = _governor(store)
    assert other.membrane.state.quarantine_active is True
    assert other.registry.quarantine_is_active() is True
    blocked = other.dispatch("op-q")
    assert blocked["dispatched"] is False
    assert "QUARANTINE_LOCK" in blocked["reason"]
    assert other.registry.get("op-q").state == OperationState.RESERVED

    refused = _release(owner, store, token="ceremony-G-R5-001")
    assert refused["released"] is False
    assert owner.registry.quarantine_is_active() is True
    released = _release(owner, store)
    assert released["released"] is True
    assert released["receipt"]["authorization_id"].startswith("test-hmac-")
    assert other.registry.quarantine_is_active() is False
    assert other.dispatch("op-q")["dispatched"] is True


def test_unconfigured_verifier_denies_release_and_direct_false_write(tmp_path):
    store = tmp_path / "ops.json"
    owner = _governor(store)
    owner.lock_quarantine("watch")
    denied = owner.release_quarantine("operator", "lei", "ceremony-G-R5-001")
    assert denied["released"] is False
    assert "not verified" in denied["reason"]
    assert owner.membrane.state.quarantine_active is True
    with pytest.raises(PermissionError, match="cannot be cleared"):
        owner.registry.set_quarantine(False)
    assert _governor(store).registry.quarantine_is_active() is True


def test_cross_process_dispatch_blocked_until_verified_release(tmp_path):
    store = tmp_path / "ops.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    _register_reserved(owner)
    owner.lock_quarantine("watch")
    ctx = multiprocessing.get_context("spawn")

    def run_child():
        queue = ctx.Queue()
        proc = ctx.Process(target=_child_dispatch, args=(str(store), "op-q", queue))
        proc.start()
        proc.join(15)
        assert proc.exitcode == 0
        return queue.get(timeout=5)

    blocked = run_child()
    assert "error" not in blocked
    assert blocked["dispatched"] is False
    assert blocked["state"] == OperationState.RESERVED.value
    assert _release(owner, store)["released"] is True
    allowed = run_child()
    assert "error" not in allowed
    assert allowed["dispatched"] is True
    assert allowed["state"] == OperationState.DISPATCHING.value


def test_dispatch_claim_and_quarantine_activation_are_atomically_ordered(tmp_path, monkeypatch):
    store = tmp_path / "ops.json"
    worker = _governor(store)
    owner = _governor(store)
    _register_reserved(owner)
    _register_reserved(owner, "op-after")
    entered = threading.Event()
    continue_claim = threading.Event()
    activation_done = threading.Event()
    outcome = {}
    original = worker.registry.transition_to

    def paused_claim(key, state, **kwargs):
        if key == "op-q" and state == OperationState.DISPATCHING:
            entered.set()
            assert continue_claim.wait(5), "failed to resume controlled claim"
        return original(key, state, **kwargs)

    monkeypatch.setattr(worker.registry, "transition_to", paused_claim)
    attempt = threading.Thread(target=lambda: outcome.update(worker.dispatch("op-q")), daemon=True)
    attempt.start()
    assert entered.wait(5)
    locker = threading.Thread(
        target=lambda: (owner.lock_quarantine("race"), activation_done.set()), daemon=True,
    )
    locker.start()
    # The quarantine activation MUST wait until an already-in-flight claim
    # has reached its durable linearization point.
    assert not activation_done.wait(0.15)
    continue_claim.set()
    attempt.join(5)
    locker.join(5)
    assert not attempt.is_alive() and not locker.is_alive()
    assert outcome["dispatched"] is True  # earlier claim remains valid
    assert activation_done.is_set()
    assert owner.registry.quarantine_is_active() is True
    assert worker.dispatch("op-after")["dispatched"] is False
    assert owner.registry.get("op-after").state == OperationState.RESERVED


def test_admission_and_quarantine_activation_are_atomically_ordered(tmp_path, monkeypatch):
    store = tmp_path / "ops.json"
    worker = _governor(store)
    owner = _governor(store)
    worker.membrane.state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    entered = threading.Event()
    proceed = threading.Event()
    activation_done = threading.Event()
    outcome = {}
    original = worker.registry.transition_to

    def paused_admission(key, state, **kwargs):
        if state == OperationState.RESERVED and key == "op-admit":
            entered.set()
            assert proceed.wait(5)
        return original(key, state, **kwargs)

    monkeypatch.setattr(worker.registry, "transition_to", paused_admission)
    passage = Passage(
        agent_id="alice", action_type="read_only", idempotency_key="op-admit",
    )
    def run_adjudication():
        outcome.update(worker.adjudicate(
            passage, pressure=0.2, velocity=0.3, capacity=1.0,
            authority=0.9, context_fit=0.9, evidence_quality=0.9,
        ))
    attempt = threading.Thread(target=run_adjudication, daemon=True)
    attempt.start()
    assert entered.wait(5), "admission did not reach its durable claim"
    locker = threading.Thread(
        target=lambda: (owner.lock_quarantine("race"), activation_done.set()),
        daemon=True,
    )
    locker.start()
    assert not activation_done.wait(0.15)
    proceed.set()
    attempt.join(5)
    locker.join(5)
    assert not attempt.is_alive() and not locker.is_alive()
    assert outcome["operation_state"] == OperationState.RESERVED.value
    assert activation_done.is_set()
    assert owner.registry.quarantine_is_active() is True
    late = worker.adjudicate(
        Passage(agent_id="alice", action_type="read_only", idempotency_key="late"),
        pressure=0.2, velocity=0.3, capacity=1.0,
        authority=0.9, context_fit=0.9, evidence_quality=0.9,
    )
    assert late["composed_decision"] == "HARD_VETO"
    assert late["operation_state"] != OperationState.RESERVED.value


def test_activation_survives_process_crash_and_restart(tmp_path):
    store = tmp_path / "ops.json"
    owner = _governor(store)
    _register_reserved(owner)
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_child_activate_then_crash, args=(str(store),))
    proc.start()
    proc.join(15)
    assert proc.exitcode == 0
    fresh = _governor(store)
    assert fresh.membrane.quarantine_active is True
    assert fresh.dispatch("op-q")["dispatched"] is False
    assert fresh.registry.get("op-q").state == OperationState.RESERVED


def test_corrupt_quarantine_mark_fails_closed_and_cannot_be_released(tmp_path):
    store = tmp_path / "ops.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    _register_reserved(owner)
    owner.lock_quarantine("watch")
    Path(str(store) + ".quarantine.json").write_text("{invalid JSON", encoding="utf-8")
    fresh = _governor(store, verifier=_trusted_test_verifier)
    assert fresh.registry.quarantine_is_active() is True
    assert fresh.dispatch("op-q")["dispatched"] is False
    refused = fresh.release_quarantine("operator", "lei", "anything")
    assert refused["released"] is False
    assert "corrupt" in refused["reason"]
    with pytest.raises(RuntimeError, match="corrupt"):
        fresh.lock_quarantine("again")
