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


def test_hold_readjudication_cannot_admit_under_shared_quarantine(tmp_path):
    store = tmp_path / "ops.json"
    gov = _governor(store)
    key = "hold-under-quarantine"
    record = OperationRecord(
        idempotency_key=key,
        passage_id="passage-held",
        agent_id="alice",
        action_type="read_only",
    )
    assert gov.registry.register(record)
    assert gov.hold_manager.enter_hold(key, tau_k_seconds=3600)["entered"]
    # A separate registry instance establishes the lock.
    other = _governor(store)
    other.lock_quarantine("cross-worker-monitor")
    result = gov.resolve_hold(
        key,
        passage=Passage(
            passage_id="passage-held", idempotency_key=key,
            agent_id="alice", action_type="read_only",
        ),
        authority=0.9, context_fit=0.9, evidence_quality=0.9,
    )
    assert result["hold_resolved"] is False
    assert "QUARANTINE_LOCK" in result["reason"]
    assert gov.registry.get(key).state == OperationState.HOLD


def test_originating_worker_observes_release_by_independent_worker(tmp_path):
    """Local lock flag must not outlive the canonical cross-process release."""
    store = tmp_path / "ops.json"
    origin = _governor(store)
    releaser = _governor(store, verifier=_trusted_test_verifier)
    _register_reserved(origin, "op-stale")
    origin.lock_quarantine("incident-watch")
    assert origin.membrane.state.quarantine is True
    assert origin.membrane.state.quarantine_active is True

    assert _release(releaser, store)["released"] is True
    # This worker did not call release_quarantine; its cache is still stale.
    assert origin.membrane.state.quarantine is True
    assert origin.registry.quarantine_is_active() is False
    assert origin.membrane.state.quarantine_active is False
    assert origin.dispatch("op-stale")["dispatched"] is True

    # A new incident on the same worker must reactivate the shared gate.
    origin.lock_quarantine("next-incident")
    assert origin.membrane.state.quarantine_active is True
    assert _governor(store).registry.quarantine_is_active() is True


def test_removed_quarantine_mark_is_not_interpreted_as_unlocked(tmp_path):
    """Deleting the active mark after initialization cannot reopen admission."""
    store = tmp_path / "ops.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    _register_reserved(owner, "op-deleted-mark")
    owner.lock_quarantine("incident")
    mark = Path(str(store) + ".quarantine.json")
    initialized = Path(str(store) + ".quarantine.initialized")
    assert mark.exists() and initialized.exists()

    mark.unlink()
    fresh = _governor(store, verifier=_trusted_test_verifier)
    assert fresh.registry.quarantine_is_active() is True
    assert fresh.dispatch("op-deleted-mark")["dispatched"] is False
    denied = fresh.release_quarantine("operator", "lei", "ceremony-G-R5-001")
    assert denied["released"] is False
    assert "corrupt" in denied["reason"]
    with pytest.raises(RuntimeError, match="corrupt"):
        fresh.lock_quarantine("second-incident")


def test_pre_sentinel_mark_is_migrated_and_deletion_fails_closed(tmp_path):
    """Old active and released marks receive a durable initialization sentinel."""
    for active in (True, False):
        store = tmp_path / f"legacy-{active}.json"
        old_mark = {
            "active": active,
            "lock_id": "legacy-quarantine-id",
        }
        if not active:
            old_mark["release_receipt"] = {
                "lock_id": "legacy-quarantine-id",
                "authorization_id": "legacy-receipt",
                "authorized_by": "legacy-operator",
                "scope": "lei",
            }
        mark = Path(str(store) + ".quarantine.json")
        mark.write_text(json.dumps(old_mark), encoding="utf-8")
        sentinel = Path(str(store) + ".quarantine.initialized")
        assert not sentinel.exists()

        migrated = DurableOperationRegistry(str(store))
        assert migrated.quarantine_is_active() is active
        assert sentinel.exists()
        mark.unlink()
        recovered = DurableOperationRegistry(str(store))
        assert recovered.quarantine_is_active() is True
        with pytest.raises(RuntimeError, match="corrupt"):
            recovered.set_quarantine(True, locked_by="must-not-reinitialize")


def test_never_initialized_quarantine_store_is_not_blocked(tmp_path):
    """An untouched registry is not mistaken for a deleted quarantine mark."""
    store = tmp_path / "brand-new-ops.json"
    fresh = DurableOperationRegistry(str(store))
    assert fresh.quarantine_is_active() is False
    assert not Path(str(store) + ".quarantine.initialized").exists()


def test_slow_verifier_does_not_block_quarantine_gate_or_local_lock(tmp_path):
    """External authentication must not hold the interprocess admission gate."""
    store = tmp_path / "ops.json"
    verification_started = threading.Event()
    finish_verification = threading.Event()

    def slow_verifier(**kwargs):
        verification_started.set()
        assert finish_verification.wait(8)
        return _trusted_test_verifier(**kwargs)

    owner = _governor(store, verifier=slow_verifier)
    observer = _governor(store)
    owner.lock_quarantine("incident")
    token = _test_token(_lock_id(store))
    result = {}
    release_thread = threading.Thread(
        target=lambda: result.update(_release(owner, store, token=token)),
        daemon=True,
    )
    release_thread.start()
    try:
        assert verification_started.wait(5), "verifier was not entered"
        observed = threading.Event()
        observation = {}
        read_thread = threading.Thread(
            target=lambda: (observation.update(active=observer.registry.quarantine_is_active()),
                            observed.set()),
            daemon=True,
        )
        read_thread.start()
        assert observed.wait(3), "slow verifier held shared quarantine gate"
        assert observation["active"] is True

        local_done = threading.Event()
        local_lock_thread = threading.Thread(
            target=lambda: (owner.lock_quarantine("same-worker-emergency"),
                            local_done.set()),
            daemon=True,
        )
        local_lock_thread.start()
        assert local_done.wait(3), "slow verifier held local quarantine mutex"
    finally:
        finish_verification.set()
        release_thread.join(8)
    assert not release_thread.is_alive()
    assert result["released"] is True
    assert result["receipt"]["authorization"] == "[redacted]"
    assert token not in json.dumps(result["receipt"])
    assert token not in Path(str(store) + ".quarantine.json").read_text(encoding="utf-8")


def test_stale_verified_unlock_cannot_release_new_quarantine_epoch(tmp_path):
    """Changing lock identity during verification invalidates the old proof."""
    store = tmp_path / "ops.json"
    verification_started = threading.Event()
    finish_verification = threading.Event()

    def slow_verifier(**kwargs):
        verification_started.set()
        assert finish_verification.wait(8)
        return _trusted_test_verifier(**kwargs)

    original = _governor(store, verifier=slow_verifier)
    competing = _governor(store, verifier=_trusted_test_verifier)
    original.lock_quarantine("first-incident")
    initial_id = _lock_id(store)
    outcome = {}
    release_thread = threading.Thread(
        target=lambda: outcome.update(_release(original, store, token=_test_token(initial_id))),
        daemon=True,
    )
    release_thread.start()
    try:
        assert verification_started.wait(5)
        assert _release(competing, store, token=_test_token(initial_id))["released"] is True
        competing.lock_quarantine("second-incident")
        assert _lock_id(store) != initial_id
    finally:
        finish_verification.set()
        release_thread.join(8)
    assert not release_thread.is_alive()
    assert outcome["released"] is False
    assert "changed during authorization" in outcome["reason"]
    assert original.registry.quarantine_is_active() is True
    assert _governor(store).registry.quarantine_is_active() is True


def test_slow_hold_kernel_does_not_delay_emergency_quarantine(tmp_path):
    """A quarantine activated mid-HOLD evaluation must retain HOLD, not admit."""
    store = tmp_path / "ops.json"
    resolver = _governor(store)
    locker = _governor(store)
    key = "slow-hold-quarantine"
    passage = Passage(
        passage_id="slow-held-passage", idempotency_key=key,
        agent_id="alice", action_type="read_only",
    )
    assert resolver.registry.register(OperationRecord(
        idempotency_key=key, passage_id=passage.passage_id,
        agent_id=passage.agent_id, action_type=passage.action_type,
    ))
    assert resolver.hold_manager.enter_hold(key, tau_k_seconds=3600)["entered"]

    evaluation_started = threading.Event()
    resume_evaluation = threading.Event()

    class SlowScoring:
        def adjudicate(self, **kwargs):
            evaluation_started.set()
            assert resume_evaluation.wait(8)
            return {"decision": "PASS", "rationale": "controlled fresh evidence"}

    resolver.scoring = SlowScoring()
    outcome = {}
    resolution = threading.Thread(
        target=lambda: outcome.update(resolver.resolve_hold(
            key, passage=passage,
            authority=0.9, context_fit=0.9, evidence_quality=0.9,
        )), daemon=True,
    )
    resolution.start()
    try:
        assert evaluation_started.wait(5)
        activation_done = threading.Event()
        activation = threading.Thread(
            target=lambda: (locker.lock_quarantine("emergency"),
                            activation_done.set()), daemon=True,
        )
        activation.start()
        assert activation_done.wait(3), "HOLD evaluation blocked emergency quarantine"
        assert locker.registry.quarantine_is_active() is True
    finally:
        resume_evaluation.set()
        resolution.join(8)
    assert not resolution.is_alive()
    assert outcome["hold_resolved"] is False
    assert outcome["composed_decision"] == "HOLD"
    assert "QUARANTINE_LOCK" in outcome["composed_rationale"]
    assert resolver.registry.get(key).state == OperationState.HOLD


def test_failed_first_mark_write_cannot_leave_false_unlocked_state(tmp_path, monkeypatch):
    """A crash/error after the durable sentinel but before mark cannot fail open."""
    store = tmp_path / "ops.json"
    original = DurableOperationRegistry(str(store))
    import tenir_governance.operations.registry as registry_module

    def fail_mark_create(*args, **kwargs):
        raise OSError("injected mark persistence failure")

    monkeypatch.setattr(registry_module.tempfile, "mkstemp", fail_mark_create)
    with pytest.raises(OSError, match="injected mark persistence failure"):
        original.set_quarantine(True, locked_by="crash-between-writes")
    monkeypatch.undo()

    assert Path(str(store) + ".quarantine.initialized").exists()
    assert not Path(str(store) + ".quarantine.json").exists()
    restarted = DurableOperationRegistry(str(store))
    assert restarted.quarantine_is_active() is True
    with pytest.raises(RuntimeError, match="corrupt"):
        restarted.set_quarantine(True, locked_by="must-not-bypass")


@pytest.mark.parametrize("malformed", [
    {"active": False},
    {"active": False, "lock_id": "lock-1"},
    {"active": False, "lock_id": "lock-1",
     "release_receipt": {"lock_id": "another-lock", "authorization_id": "a",
                         "authorized_by": "operator", "scope": "lei"}},
    {"active": False, "lock_id": "lock-1",
     "release_receipt": {"lock_id": "lock-1", "authorization_id": "",
                         "authorized_by": "operator", "scope": "lei"}},
])
def test_malformed_inactive_mark_never_unlocks(tmp_path, malformed):
    """An inactive state without a matching authorization receipt fails closed."""
    store = tmp_path / "ops.json"
    reg = DurableOperationRegistry(str(store))
    assert reg.quarantine_is_active() is False
    mark = Path(str(store) + ".quarantine.json")
    mark.write_text(json.dumps(malformed), encoding="utf-8")
    recovered = DurableOperationRegistry(str(store))
    assert recovered.quarantine_is_active() is True
    with pytest.raises(RuntimeError, match="corrupt"):
        recovered.set_quarantine(True)


def test_distinct_incident_renews_active_lock_and_invalidates_pending_release(tmp_path):
    """An independent incident must not be cleared by proof for the old epoch."""
    store = tmp_path / "ops.json"
    verification_started = threading.Event()
    complete_verification = threading.Event()

    def suspended_verifier(**kwargs):
        verification_started.set()
        assert complete_verification.wait(8)
        return _trusted_test_verifier(**kwargs)

    original = _governor(store, verifier=suspended_verifier)
    other = _governor(store, verifier=_trusted_test_verifier)
    original.lock_quarantine("incident-operator", incident_id="case-001")
    first_lock_id = _lock_id(store)

    result = {}
    pending = threading.Thread(
        target=lambda: result.update(
            _release(original, store, token=_test_token(first_lock_id))
        ),
        daemon=True,
    )
    pending.start()
    try:
        assert verification_started.wait(5)
        # Same incident ID is idempotent, including from another worker.
        other.membrane.lock_quarantine("another-operator", incident_id="case-001")
        assert _lock_id(store) == first_lock_id
        # A distinct incident while release verification is in progress
        # must mint a fresh lock identity before the old proof can commit.
        other.lock_quarantine("incident-operator", incident_id="case-002")
        second_lock_id = _lock_id(store)
        assert second_lock_id != first_lock_id
        assert other.registry.quarantine_is_active() is True
    finally:
        complete_verification.set()
        pending.join(8)

    assert not pending.is_alive()
    assert result["released"] is False
    assert "changed during authorization" in result["reason"]
    assert other.registry.quarantine_is_active() is True
    assert _release(other, store, token=_test_token(first_lock_id))["released"] is False
    fresh = _release(other, store, token=_test_token(second_lock_id))
    assert fresh["released"] is True
    assert fresh["receipt"]["incident_id"] == "case-002"
    assert original.registry.quarantine_is_active() is False


@pytest.mark.parametrize("invalid", ["", " ", 123, False, {}, []])
def test_invalid_incident_id_cannot_change_quarantine(tmp_path, invalid):
    store = tmp_path / "ops.json"
    governor = _governor(store)
    governor.lock_quarantine("watch", incident_id="case-001")
    original = _lock_id(store)
    with pytest.raises(ValueError, match="incident_id"):
        governor.lock_quarantine("watch", incident_id=invalid)
    assert _lock_id(store) == original
    assert governor.registry.quarantine_is_active() is True


def test_unbound_membrane_new_incident_also_invalidates_old_proof():
    """Standalone MembraneState must honor incident epochs without a registry."""
    state = MembraneState(quarantine_release_verifier=_trusted_test_verifier)
    state.lock_quarantine("local-operator", incident_id="local-001")
    first = state._quarantine_lock_id
    state.lock_quarantine("local-operator", incident_id="local-001")
    assert state._quarantine_lock_id == first

    state.lock_quarantine("local-operator", incident_id="local-002")
    second = state._quarantine_lock_id
    assert first != second
    assert state.quarantine_active is True
    denied = state.release_quarantine(
        "operator", "lei", _test_token(first),
    )
    assert denied["released"] is False
    assert state.quarantine_active is True
    accepted = state.release_quarantine(
        "operator", "lei", _test_token(second),
    )
    assert accepted["released"] is True
    assert accepted["receipt"]["incident_id"] == "local-002"
    assert state.quarantine_active is False
    assert state._quarantine_incident_id is None


@pytest.mark.parametrize("incident_id", ["local-001", "local-002"])
def test_unbound_release_rechecks_incident_after_verified_callback(incident_id):
    """A reentrant verifier cannot use an old proof to clear a fresh incident."""
    state = MembraneState()
    state.lock_quarantine("initial-watch", incident_id="local-001")
    first = state._quarantine_lock_id
    during_verification = {}

    def verify_then_report_incident(**request):
        proof = _trusted_test_verifier(**request)
        assert proof is not None
        state.lock_quarantine("callback-watch", incident_id=incident_id)
        during_verification.update({
            "quarantine": state.quarantine,
            "lock_id": state._quarantine_lock_id,
            "incident_id": state._quarantine_incident_id,
            "locked_at": state.quarantine_locked_at,
            "locked_by": state.quarantine_locked_by,
            "receipt": state.quarantine_release_receipt,
        })
        return proof

    state.quarantine_release_verifier = verify_then_report_incident
    result = state.release_quarantine("operator", "lei", _test_token(first))

    if incident_id == "local-001":
        # Reporting the same incident keeps its epoch and valid authorization.
        assert during_verification["lock_id"] == first
        assert result["released"] is True
        assert result["receipt"]["lock_id"] == first
        assert result["receipt"]["incident_id"] == incident_id
        assert state.quarantine_active is False
    else:
        assert during_verification["lock_id"] != first
        assert result["released"] is False
        assert result["receipt"] is None
        assert "changed during authorization" in result["reason"]
        assert {
            "quarantine": state.quarantine,
            "lock_id": state._quarantine_lock_id,
            "incident_id": state._quarantine_incident_id,
            "locked_at": state.quarantine_locked_at,
            "locked_by": state.quarantine_locked_by,
            "receipt": state.quarantine_release_receipt,
        } == during_verification
        # A fresh proof remains usable after the stale release is refused.
        state.quarantine_release_verifier = _trusted_test_verifier
        accepted = state.release_quarantine(
            "operator", "lei", _test_token(during_verification["lock_id"]),
        )
        assert accepted["released"] is True
        assert accepted["receipt"]["incident_id"] == incident_id


def test_prebound_incident_identity_is_preserved_on_registry_binding(tmp_path):
    """Attaching a durable store to a locked local membrane retains incident scope."""
    store = tmp_path / "ops.json"
    state = MembraneState()
    state.lock_quarantine("prebind-watch", incident_id="case-before-bind")
    registry = DurableOperationRegistry(str(store))
    state.bind_quarantine_registry(registry)
    mark = json.loads(Path(str(store) + ".quarantine.json").read_text())
    assert mark["active"] is True
    assert mark["incident_id"] == "case-before-bind"
    assert mark["locked_by"] == "prebind-watch"
    assert state.quarantine_active is True


@pytest.mark.parametrize("persistent", [False, True])
def test_direct_registry_claims_cannot_bypass_quarantine(tmp_path, persistent):
    """Public transition_to must obey I6 without a governor or membrane."""
    store = str(tmp_path / "direct-claims.json") if persistent else None
    reg = DurableOperationRegistry(store)
    for key in ("direct-created", "direct-reserved", "direct-held"):
        assert reg.register(OperationRecord(
            idempotency_key=key, passage_id=f"p-{key}",
            agent_id="alice", action_type="payment",
        ))
    assert reg.transition_to("direct-reserved", OperationState.RESERVED)[0]
    assert reg.transition_to("direct-held", OperationState.HOLD)[0]
    reg.set_quarantine(True, locked_by="emergency", incident_id="direct-case")

    for key, target, prior in (
        ("direct-created", OperationState.RESERVED, OperationState.CREATED),
        ("direct-reserved", OperationState.DISPATCHING, OperationState.RESERVED),
        ("direct-held", OperationState.RESERVED, OperationState.HOLD),
    ):
        admitted, seen_state = reg.transition_to(key, target)
        assert admitted is False
        assert seen_state == prior
        assert reg.get(key).state == prior

    # Fail-closed transitions must remain possible during quarantine.
    assert reg.transition_to("direct-held", OperationState.FAILED)[0]
    assert reg.get("direct-held").state == OperationState.FAILED


def test_hold_manager_authorized_exit_cannot_override_quarantine(tmp_path):
    """An exit receipt is not authority to ignore LEI quarantine I6."""
    from tenir_governance.operations.hold import HoldManager

    store = tmp_path / "exit-guard.json"
    reg = DurableOperationRegistry(str(store))
    key = "escalated-under-quarantine"
    assert reg.register(OperationRecord(
        idempotency_key=key, passage_id="p-escalated",
        agent_id="alice", action_type="payment",
    ))
    manager = HoldManager(reg)
    assert manager.enter_hold(key, tau_k_seconds=3600)["entered"]
    assert manager.escalate_hold(key, escalated_by="risk-officer")["escalated"]
    original = reg.get(key).hold_record

    reg.set_quarantine(True, locked_by="incident", incident_id="case-exit")
    blocked = manager.authorize_exit(
        key, authorized_by="risk-officer", scope="payment",
        authorization="ceremony-G-R5-001", approve=True,
    )
    assert blocked["exited"] is False
    assert blocked["exit_receipt"] is None
    assert blocked["operation_state"] == OperationState.ESCALATED.value
    after = reg.get(key)
    assert after.hold_record.exit_receipt is None
    assert after.hold_record.exit_reason == original.exit_reason

    # Denying the unsafe operation remains available under quarantine.
    denied = manager.authorize_exit(
        key, authorized_by="risk-officer", scope="payment",
        authorization="ceremony-G-R5-001", approve=False,
    )
    assert denied["exited"] is True
    assert reg.get(key).state == OperationState.FAILED


def test_direct_claim_and_activation_serialize_under_shared_gate(tmp_path, monkeypatch):
    """A direct transition that began first must finish before lock activation."""
    store = tmp_path / "direct-race.json"
    claimant = DurableOperationRegistry(str(store))
    locker = DurableOperationRegistry(str(store))
    key = "direct-race"
    assert claimant.register(OperationRecord(
        idempotency_key=key, passage_id="p-direct-race",
        agent_id="alice", action_type="payment",
    ))
    entered = threading.Event()
    proceed = threading.Event()
    activation_complete = threading.Event()
    original = claimant._transition_to_unchecked_quarantine
    output = {}

    def paused_claim(*args, **kwargs):
        if args[1] == OperationState.RESERVED:
            entered.set()
            assert proceed.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(claimant, "_transition_to_unchecked_quarantine", paused_claim)
    claiming = threading.Thread(
        target=lambda: output.update(success=claimant.transition_to(key, OperationState.RESERVED)[0]),
        daemon=True,
    )
    claiming.start()
    assert entered.wait(5)
    activation = threading.Thread(
        target=lambda: (locker.set_quarantine(True, incident_id="race-incident"),
                        activation_complete.set()), daemon=True,
    )
    activation.start()
    assert not activation_complete.wait(0.15)
    proceed.set()
    claiming.join(5)
    activation.join(5)
    assert not claiming.is_alive() and not activation.is_alive()
    assert output["success"] is True
    assert activation_complete.is_set()
    assert locker.quarantine_is_active()
    assert claimant.transition_to(key, OperationState.DISPATCHING)[0] is False
    assert locker.get(key).state == OperationState.RESERVED


def _child_crash_after_inactive_quarantine_write(store_path):
    """Spawned worker: simulated crash before release-pending cleanup."""
    reg = DurableOperationRegistry(str(store_path), quarantine_release_verifier=_trusted_test_verifier)
    original = reg._quarantine_write_locked

    def write_then_crash(mark):
        original(mark)
        if mark["active"] is False:
            os._exit(0)

    reg._quarantine_write_locked = write_then_crash
    reg.authorized_release_quarantine(
        authorized_by="operator", scope="lei",
        authorization=_test_token(_lock_id(store_path)), reason="test crash window",
    )


def test_release_mark_write_error_remains_locked_across_workers(tmp_path, monkeypatch):
    """An error after inactive os.replace cannot expose an unlocked store."""
    store = tmp_path / "release-write-failed.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    _register_reserved(owner, "pending-release")
    owner.lock_quarantine("watch")
    original = owner.registry._quarantine_write_locked

    def write_then_fail(mark):
        original(mark)
        if mark["active"] is False:
            raise OSError("simulated directory fsync failure after replace")

    monkeypatch.setattr(owner.registry, "_quarantine_write_locked", write_then_fail)
    with pytest.raises(OSError, match="simulated directory fsync failure"):
        _release(owner, store)
    pending = Path(str(store) + ".quarantine.release-pending")
    assert pending.exists()
    assert json.loads(Path(str(store) + ".quarantine.json").read_text())["active"] is False

    second = _governor(store, verifier=_trusted_test_verifier)
    assert second.registry.quarantine_is_active() is True
    assert second.dispatch("pending-release")["dispatched"] is False
    refused = _release(second, store)
    assert refused["released"] is False
    assert "corrupt" in refused["reason"]
    with pytest.raises(RuntimeError, match="corrupt"):
        second.lock_quarantine("must-not-clear")


def test_release_crash_after_inactive_write_remains_locked_on_restart(tmp_path):
    """The on-disk pending sidecar survives an independent process crash."""
    store = tmp_path / "release-crash.json"
    owner = _governor(store)
    _register_reserved(owner, "crash-release")
    owner.lock_quarantine("watch")
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_child_crash_after_inactive_quarantine_write, args=(str(store),))
    child.start()
    child.join(15)
    assert child.exitcode == 0

    assert Path(str(store) + ".quarantine.release-pending").exists()
    restarted = _governor(store)
    assert restarted.registry.quarantine_is_active() is True
    assert restarted.dispatch("crash-release")["dispatched"] is False


def test_successful_quarantine_release_cleans_pending_guard(tmp_path):
    store = tmp_path / "release-guard-complete.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    owner.lock_quarantine("watch", incident_id="guard-test")
    released = _release(owner, store)
    assert released["released"] is True
    assert Path(str(store) + ".quarantine.initialized").exists()
    assert not Path(str(store) + ".quarantine.release-pending").exists()
    assert _governor(store).registry.quarantine_is_active() is False


def test_release_failure_before_inactive_mark_never_clears_lock(tmp_path, monkeypatch):
    """Crash/failure immediately after pending is durable leaves active quarantine."""
    store = tmp_path / "release-before-mark.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    owner.lock_quarantine("incident")
    original = owner.registry._quarantine_write_locked

    def fail_before_replace(mark):
        if mark["active"] is False:
            raise OSError("injected before inactive replace")
        return original(mark)

    monkeypatch.setattr(owner.registry, "_quarantine_write_locked", fail_before_replace)
    with pytest.raises(OSError, match="injected before inactive replace"):
        _release(owner, store)

    pending = Path(str(store) + ".quarantine.release-pending")
    assert pending.exists()
    raw = json.loads(Path(str(store) + ".quarantine.json").read_text())
    assert raw["active"] is True
    fresh = _governor(store)
    assert fresh.registry.quarantine_is_active() is True
    assert fresh.release_quarantine("operator", "lei", "ceremony-G-R5-001")["released"] is False


def _child_direct_registry_transition(store_path, key, target_name, queue):
    """Independent Python interpreter calling the registry without a governor."""
    try:
        registry = DurableOperationRegistry(str(store_path))
        success, previous = registry.transition_to(key, OperationState(target_name))
        latest = registry.get(key)
        queue.put({
            "success": success,
            "previous": previous.value if previous else None,
            "state": latest.state.value if latest else None,
        })
    except BaseException as exc:
        queue.put({"error": repr(exc)})


def test_independent_process_direct_claim_obeys_shared_quarantine(tmp_path):
    """Direct durable registry users cannot bypass lock from a second process."""
    store = tmp_path / "raw-process-claims.json"
    owner = _governor(store, verifier=_trusted_test_verifier)
    assert owner.registry.register(OperationRecord(
        idempotency_key="created-cross-process", passage_id="created-p",
        agent_id="alice", action_type="payment",
    ))
    _register_reserved(owner, "reserved-cross-process")
    owner.lock_quarantine("watch", incident_id="case-cross-process")
    ctx = multiprocessing.get_context("spawn")

    def attempt(key, target):
        queue = ctx.Queue()
        worker = ctx.Process(
            target=_child_direct_registry_transition,
            args=(str(store), key, target.value, queue),
        )
        worker.start()
        worker.join(15)
        assert worker.exitcode == 0
        return queue.get(timeout=5)

    created = attempt("created-cross-process", OperationState.RESERVED)
    assert "error" not in created
    assert created == {
        "success": False,
        "previous": OperationState.CREATED.value,
        "state": OperationState.CREATED.value,
    }
    dispatch = attempt("reserved-cross-process", OperationState.DISPATCHING)
    assert "error" not in dispatch
    assert dispatch == {
        "success": False,
        "previous": OperationState.RESERVED.value,
        "state": OperationState.RESERVED.value,
    }

    assert _release(owner, store)["released"] is True
    admitted = attempt("created-cross-process", OperationState.RESERVED)
    assert admitted["success"] is True
    assert admitted["state"] == OperationState.RESERVED.value


@pytest.mark.parametrize("blocked_call", ["get_state", "commit"])
def test_slow_durable_backend_does_not_block_emergency_quarantine(
    tmp_path, blocked_call,
):
    """Only the dispatch claim, not remote budget I/O, holds the shared gate."""
    from types import SimpleNamespace

    store = tmp_path / f"backend-block-{blocked_call}.json"
    claimant = _governor(store)
    locker = _governor(store)
    key = "backend-blocked-op"
    assert claimant.registry.register(OperationRecord(
        idempotency_key=key, passage_id="p-backend-blocked",
        agent_id="alice", action_type="payment",
        resource_id="budget", amount=10.0,
    ))
    assert claimant.registry.set_budget_flags(
        key, budget_reserved=True, reservation_id="reservation-test-id",
    )
    assert claimant.registry.transition_to(key, OperationState.RESERVED)[0]

    entered = threading.Event()
    resume_backend = threading.Event()
    class PausedBackend:
        def _pause(self, which):
            if which == blocked_call:
                entered.set()
                assert resume_backend.wait(8), f"{which} remained blocked"

        def get_state(self, resource_id):
            assert resource_id == "budget"
            self._pause("get_state")
            return SimpleNamespace(reservations=[
                {"reservation_id": "reservation-test-id", "state": "RESERVED"},
            ])

        def commit(self, reservation_id):
            assert reservation_id == "reservation-test-id"
            self._pause("commit")
            return SimpleNamespace(status="COMMITTED")

    claimant.consensus_backend = PausedBackend()
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.update(claimant.dispatch(key)), daemon=True,
    )
    worker.start()
    try:
        assert entered.wait(5), "backend did not reach expected blocked call"
        assert claimant.registry.get(key).state == OperationState.DISPATCHING
        activated = threading.Event()
        emergency = threading.Thread(
            target=lambda: (locker.lock_quarantine("emergency", incident_id="new-incident"),
                            activated.set()), daemon=True,
        )
        emergency.start()
        assert activated.wait(3), f"quarantine blocked by backend.{blocked_call}()"
        assert locker.registry.quarantine_is_active() is True
        assert locker.registry.get(key).state == OperationState.DISPATCHING
    finally:
        resume_backend.set()
        worker.join(8)

    assert not worker.is_alive()
    assert outcome["dispatched"] is True
    assert claimant.registry.get(key).state == OperationState.DISPATCHING
    assert claimant.registry.get(key).budget_committed is True
    assert locker.registry.quarantine_is_active() is True
    assert locker.dispatch(key)["dispatched"] is False


def test_failed_claimed_backend_commit_does_not_expose_provider_execution(tmp_path):
    """Backend uncertainty after the durable claim never reports dispatch success."""
    from types import SimpleNamespace

    store = tmp_path / "backend-rejected-commit.json"
    gov = _governor(store)
    key = "backend-commit-refused"
    assert gov.registry.register(OperationRecord(
        idempotency_key=key, passage_id="p-commit-refused",
        agent_id="alice", action_type="payment",
        resource_id="budget", amount=10.0,
    ))
    assert gov.registry.set_budget_flags(
        key, budget_reserved=True, reservation_id="reservation-refused",
    )
    assert gov.registry.transition_to(key, OperationState.RESERVED)[0]

    class RefusingBackend:
        def get_state(self, resource_id):
            return SimpleNamespace(reservations=[
                {"reservation_id": "reservation-refused", "state": "RESERVED"},
            ])

        def commit(self, reservation_id):
            return SimpleNamespace(status="REFUSED")

    gov.consensus_backend = RefusingBackend()
    result = gov.dispatch(key)
    assert result["dispatched"] is False
    assert result["operation_state"] == OperationState.DISPATCHING.value
    assert "refused commit" in result["reason"]
    assert gov.registry.get(key).budget_committed is False
