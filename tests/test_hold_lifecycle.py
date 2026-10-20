# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Tests for the HOLD lifecycle — governed state for suspended operations.

Per doctrine v7.1 and Blueprint v4.4:
    HOLD is a first-class protocol state — a designed suspension of agency
    under pressure, not a system failure.

Tests cover:
    1. HOLD lifecycle: enter → resolve, escalate, expire
    2. ESCALATED: absorbing state, authorized exit, τ_K expiry → UNKNOWN
    3. QUARANTINE_LOCK: lock, authenticated release, receipt
    4. Composition-level HOLD: strategies, progressive unblock
    5. τ_K temporal validity bound checking
    6. Exit receipts
"""
import time

import pytest
from tenir_governance.operations import (
    DurableOperationRegistry, OperationRecord, OperationState,
    HoldRecord, ExitReceipt, HoldManager,
)
from tenir_governance.composition import (
    DualKernelGovernor, ScoringKernelAdapter,
    CompositionHoldStrategy, CompositionHoldManager,
)
from tenir_governance.composition.decisions import Decision
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.objects import Agent, Passage, Resource
from tenir_kernel.kernel import TenirKernel


def _setup_governor(budget_capacity=100.0, policy=None, registry=None):
    state = MembraneState()
    budget = Resource(resource_id="budget", capacity=budget_capacity)
    state.register_resource(budget)
    alice = Agent(agent_id="alice", authority_score=0.9,
                  trusted_delegates=frozenset({"bob"}))
    state.register_agent(alice)
    tenir_kernel = TenirKernel()
    scoring = ScoringKernelAdapter(tenir_kernel)
    membrane = TransitionMembrane(state)
    return DualKernelGovernor(scoring, membrane, policy=policy, registry=registry), state


def _identity_hold_fixture(payload=None):
    """Create a pending HOLD with immutable, durably recorded action identity."""
    from tenir_governance.operations.payload_binding import canonical_payload_hash

    gov, _ = _setup_governor()
    original = Passage(
        passage_id="hold-identity-passage-1",
        idempotency_key="hold-identity-operation-1",
        agent_id="alice",
        action_type="payment",
    )
    assert gov.registry.register(OperationRecord(
        idempotency_key=original.idempotency_key,
        passage_id=original.passage_id,
        agent_id=original.agent_id,
        action_type=original.action_type,
        payload_hash=canonical_payload_hash(payload),
    ))
    assert gov.hold_manager.enter_hold(original.idempotency_key, tau_k_seconds=3600)["entered"]

    class AlwaysPassScoring:
        def adjudicate(self, **kwargs):
            return {"decision": Decision.PASS.value, "rationale": "controlled fixture"}

    gov.scoring = AlwaysPassScoring()
    return gov, original


def _resolve_identity_hold(gov, key, passage, **kwargs):
    return gov.resolve_hold(
        key, passage=passage,
        authority=0.9, context_fit=0.9, evidence_quality=0.9,
        **kwargs,
    )


@pytest.mark.parametrize("overrides,field", [
    ({"idempotency_key": "forged-operation"}, "idempotency_key"),
    ({"passage_id": "forged-passage"}, "passage_id"),
    ({"agent_id": "bob"}, "agent_id"),
    ({"action_type": "infrastructure_change"}, "action_type"),
])
def test_hold_rejects_substituted_durable_identity_without_mutation(overrides, field):
    gov, original = _identity_hold_fixture()
    identity = {
        "idempotency_key": original.idempotency_key,
        "passage_id": original.passage_id,
        "agent_id": original.agent_id,
        "action_type": original.action_type,
    }
    identity.update(overrides)
    attempted = Passage(**identity)
    result = _resolve_identity_hold(gov, original.idempotency_key, attempted)

    assert result["hold_resolved"] is False
    assert result["operation_state"] == OperationState.HOLD.value
    assert field in result["reason"]
    assert gov.registry.get(original.idempotency_key).state == OperationState.HOLD
    assert attempted.idempotency_key == identity["idempotency_key"]
    assert gov.ledger.verify_chain()


@pytest.mark.parametrize("supplied_payload", [
    None,
    {"beneficiary": "mallory", "amount": 10},
    {"beneficiary": "bob", "amount": 10, "extra": "unapproved"},
])
def test_hold_rejects_missing_or_changed_authorized_payload(supplied_payload):
    authorized_payload = {"beneficiary": "bob", "amount": 10}
    gov, original = _identity_hold_fixture(payload=authorized_payload)
    result = _resolve_identity_hold(
        gov, original.idempotency_key, original, payload=supplied_payload,
    )

    assert result["hold_resolved"] is False
    assert result["operation_state"] == OperationState.HOLD.value
    assert "payload mismatch" in result["reason"]
    assert gov.registry.get(original.idempotency_key).state == OperationState.HOLD
    assert gov.ledger.verify_chain()


def test_hold_matching_payload_and_identity_re_adjudicates_normally():
    authorized_payload = {"beneficiary": "bob", "amount": 10}
    gov, original = _identity_hold_fixture(payload=authorized_payload)
    result = _resolve_identity_hold(
        gov, original.idempotency_key, original,
        payload={"amount": 10, "beneficiary": "bob"},
    )

    assert result["hold_resolved"] is True
    assert result["operation_state"] == OperationState.RESERVED.value
    assert gov.registry.get(original.idempotency_key).state == OperationState.RESERVED



@pytest.mark.parametrize("persistent", [False, True])
def test_failed_escalation_does_not_leave_false_exit_metadata(monkeypatch, tmp_path, persistent):
    """A losing HOLD -> ESCALATED race must not persist an escalation marker."""
    store_path = tmp_path / "escalation.json" if persistent else None
    reg = DurableOperationRegistry(str(store_path) if store_path else None)
    key = "hold-escalation-lost-race"
    assert reg.register(OperationRecord(
        idempotency_key=key, passage_id="p-race",
        agent_id="alice", action_type="payment",
    ))
    hm = HoldManager(reg)
    assert hm.enter_hold(key, tau_k_seconds=3600)["entered"]
    original = reg.transition_to

    def lose_race(idempotency_key, target, *args, **kwargs):
        if target == OperationState.ESCALATED:
            # Another worker wins HOLD -> FAILED before this transition.
            assert original(idempotency_key, OperationState.FAILED)[0]
            return False, OperationState.FAILED
        return original(idempotency_key, target, *args, **kwargs)

    monkeypatch.setattr(reg, "transition_to", lose_race)
    result = hm.escalate_hold(key, escalated_by="risk-officer")

    assert result["escalated"] is False
    assert result["operation_state"] == OperationState.FAILED.value
    final = reg.get(key)
    assert final.state == OperationState.FAILED
    assert final.hold_record.exit_reason != "escalated"
    assert not final.hold_record.escalated_at
    if store_path:
        persisted = DurableOperationRegistry(str(store_path)).get(key)
        assert persisted.state == OperationState.FAILED
        assert persisted.hold_record.exit_reason != "escalated"
        assert not persisted.hold_record.escalated_at


@pytest.mark.parametrize("persistent", [False, True])
def test_failed_authorized_exit_does_not_leave_false_receipt(monkeypatch, tmp_path, persistent):
    """A losing ESCALATED exit must not leave an authorization receipt."""
    store_path = tmp_path / "authorized-exit.json" if persistent else None
    reg = DurableOperationRegistry(str(store_path) if store_path else None)
    key = "hold-authorization-lost-race"
    assert reg.register(OperationRecord(
        idempotency_key=key, passage_id="p-race",
        agent_id="alice", action_type="payment",
    ))
    hm = HoldManager(reg)
    assert hm.enter_hold(key, tau_k_seconds=3600)["entered"]
    assert hm.escalate_hold(key, escalated_by="risk-officer")["escalated"]
    original = reg.transition_to

    def lose_race(idempotency_key, target, *args, **kwargs):
        if target == OperationState.RESERVED:
            assert original(idempotency_key, OperationState.UNKNOWN)[0]
            return False, OperationState.UNKNOWN
        return original(idempotency_key, target, *args, **kwargs)

    monkeypatch.setattr(reg, "transition_to", lose_race)
    result = hm.authorize_exit(
        key, authorized_by="officer-1", scope="payment",
        authorization="ceremony-G-R5-001", approve=True,
    )

    assert result["exited"] is False
    assert result["operation_state"] == OperationState.UNKNOWN.value
    assert result["exit_receipt"] is None
    final = reg.get(key)
    assert final.state == OperationState.UNKNOWN
    assert final.hold_record.exit_reason != "authorized_exit"
    assert final.hold_record.exit_receipt is None
    if store_path:
        persisted = DurableOperationRegistry(str(store_path)).get(key)
        assert persisted.state == OperationState.UNKNOWN
        assert persisted.hold_record.exit_reason != "authorized_exit"
        assert persisted.hold_record.exit_receipt is None


@pytest.mark.parametrize("persistent", [False, True])
def test_failed_transition_write_keeps_state_and_hold_metadata_paired(monkeypatch, tmp_path, persistent):
    """A failed registry write must not leave an in-memory half transition."""
    store_path = tmp_path / "failed-write.json" if persistent else None
    reg = DurableOperationRegistry(str(store_path) if store_path else None)
    key = "hold-escalation-write-failure"
    assert reg.register(OperationRecord(
        idempotency_key=key, passage_id="p-write-failure",
        agent_id="alice", action_type="payment",
    ))
    hm = HoldManager(reg)
    assert hm.enter_hold(key, tau_k_seconds=3600)["entered"]

    def fail_persist():
        raise OSError("simulated failure before durable replacement")

    monkeypatch.setattr(reg, "_persist", fail_persist)
    with pytest.raises(OSError, match="simulated failure"):
        hm.escalate_hold(key, escalated_by="risk-officer")

    final = reg.get(key)
    assert final.state == OperationState.HOLD
    assert final.hold_record.exit_reason == ""
    assert final.hold_record.escalated_at == ""
    if store_path:
        persisted = DurableOperationRegistry(str(store_path)).get(key)
        assert persisted.state == OperationState.HOLD
        assert persisted.hold_record.exit_reason == ""
        assert persisted.hold_record.escalated_at == ""


def test_durable_registry_reload_uses_newer_hold_metadata_from_disk(tmp_path):
    """A warm registry instance must not mask another instance's commit."""
    store_path = tmp_path / "shared-registry.json"
    first = DurableOperationRegistry(str(store_path))
    second = DurableOperationRegistry(str(store_path))
    key = "hold-metadata-second-reader"
    assert first.register(OperationRecord(
        idempotency_key=key, passage_id="p-shared",
        agent_id="alice", action_type="payment",
    ))
    first_hold = HoldManager(first)
    second.get(key)  # Cache the pre-HOLD record in the second instance.

    assert first_hold.enter_hold(key, tau_k_seconds=3600)["entered"]
    assert second.get(key).state == OperationState.HOLD
    assert first_hold.escalate_hold(key, escalated_by="risk-officer")["escalated"]

    reloaded = second.get(key)
    assert reloaded.state == OperationState.ESCALATED
    assert reloaded.hold_record.exit_reason == "escalated"
    assert reloaded.hold_record.escalated_by == "risk-officer"



# ── HOLD Lifecycle Tests ─────────────────────────────────────────────

class TestHoldLifecycle:
    """Test the HOLD lifecycle: enter → resolve/escalate/expire."""

    def test_adjudicate_hold_initializes_lifecycle_record(self):
        """A fail-closed HOLD decision must enter through HoldManager."""
        gov, _state = _setup_governor()

        class BrokenScoring:
            def adjudicate(self, **kwargs):
                raise RuntimeError("simulated scoring failure")

        gov.scoring = BrokenScoring()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = gov.adjudicate(
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )

        assert result["composed_decision"] == Decision.HOLD.value
        assert result["operation_state"] == OperationState.HOLD.value
        record = gov.registry.get(p.idempotency_key)
        assert record is not None
        assert record.hold_record is not None
        assert record.hold_record.hold_start

    def test_hold_state_in_registry(self):
        """An operation in HOLD state is visible in the registry."""
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = gov.adjudicate(
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        # If the decision is HOLD, check the state
        if result["composed_decision"] == Decision.HOLD.value:
            assert result["operation_state"] == OperationState.HOLD.value
            record = gov.registry.get(p.idempotency_key)
            assert record.state == OperationState.HOLD
            assert record.hold_record is not None

    def test_hold_manager_enter_hold(self):
        """HoldManager.enter_hold() transitions CREATED → HOLD."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-1", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        result = hm.enter_hold("op-hold-1", tau_k_seconds=300, tau_k_source="on-call SLA P1")
        assert result["entered"] is True
        assert result["operation_state"] == OperationState.HOLD.value
        assert result["hold_record"]["tau_k_seconds"] == 300
        assert result["hold_record"]["tau_k_source"] == "on-call SLA P1"
        assert result["hold_record"]["hold_start"] != ""

    def test_hold_manager_scenario_tau_k_is_one_hour_when_explicit(self):
        """Scenario callers can explicitly set a meaningful one-hour horizon."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-default-tau", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)

        result = hm.enter_hold(
            "op-hold-default-tau",
            tau_k_seconds=3600.0,
            tau_k_source="scenario default (1 hour)",
        )

        assert result["entered"] is True
        assert result["hold_record"]["tau_k_seconds"] == 3600.0
        assert result["hold_record"]["tau_k_source"] == "scenario default (1 hour)"

    def test_hold_manager_generic_tau_k_is_unset_by_default(self):
        """Generic lifecycle entry must not invent an organization SLA."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-no-default-tau", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        result = HoldManager(reg).enter_hold("op-hold-no-default-tau")

        assert result["entered"] is True
        assert result["hold_record"]["tau_k_seconds"] == 0.0
        assert result["hold_record"]["tau_k_source"] == ""

    def test_hold_manager_enter_hold_wrong_state(self):
        """enter_hold() fails if operation is not in CREATED state."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-2", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        reg.transition_to("op-hold-2", OperationState.RESERVED)
        hm = HoldManager(reg)
        result = hm.enter_hold("op-hold-2")
        assert result["entered"] is False
        assert "not CREATED" in result["reason"]

    def test_hold_manager_enter_hold_not_found(self):
        """enter_hold() returns not_found for unknown keys."""
        reg = DurableOperationRegistry()
        hm = HoldManager(reg)
        result = hm.enter_hold("nonexistent")
        assert result["entered"] is False
        assert "not found" in result["reason"]

    def test_hold_manager_direct_resolution_is_rejected(self):
        """HoldManager alone must not bypass fresh governor re-adjudication."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-3", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-hold-3", tau_k_seconds=300)

        result = hm.resolve_hold("op-hold-3")

        assert result["resolved"] is False
        assert result["operation_state"] == OperationState.HOLD.value
        assert "Governor.resolve_hold()" in result["reason"]
        record = reg.get("op-hold-3")
        assert record.state == OperationState.HOLD
        assert record.hold_record.exit_reason == ""

    def test_hold_manager_resolve_hold_wrong_state(self):
        """resolve_hold() fails if operation is not in HOLD state."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-4", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        result = hm.resolve_hold("op-hold-4")
        assert result["resolved"] is False
        assert "not HOLD" in result["reason"]

    def test_hold_manager_escalate_hold(self):
        """HoldManager.escalate_hold() transitions HOLD → ESCALATED."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-5", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-hold-5", tau_k_seconds=300)
        result = hm.escalate_hold("op-hold-5", escalated_by="risk-officer@org")
        assert result["escalated"] is True
        assert result["operation_state"] == OperationState.ESCALATED.value
        record = reg.get("op-hold-5")
        assert record.hold_record.escalated_by == "risk-officer@org"
        assert record.hold_record.escalated_at != ""
        assert record.hold_record.exit_reason == "escalated"

    def test_hold_manager_expire_hold(self, monkeypatch):
        """HoldManager.expire_hold() transitions HOLD → FAILED (fail-closed)."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-6", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-hold-6", tau_k_seconds=300)
        # Expiry is now enforced against τ_K; make the operation older than
        # its bound instead of assuming that calling expire_hold forces it.
        from tenir_governance.operations import hold as hold_module
        monkeypatch.setattr(hold_module, "_utc_now_seconds", lambda: time.time() + 301)
        result = hm.expire_hold("op-hold-6")
        assert result["expired"] is True
        assert result["operation_state"] == OperationState.FAILED.value
        record = reg.get("op-hold-6")
        assert record.hold_record.exit_reason == "expired"

    def test_hold_manager_expire_escalated(self):
        """HoldManager.expire_escalated() transitions ESCALATED → UNKNOWN.

        Per v7.1 §19: time-based exit from ESCALATED is rejected.
        Expiry produces UNKNOWN, not resolution.
        """
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-hold-7", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-hold-7", tau_k_seconds=300)
        hm.escalate_hold("op-hold-7")
        result = hm.expire_escalated("op-hold-7")
        assert result["expired"] is True
        assert result["operation_state"] == OperationState.UNKNOWN.value
        record = reg.get("op-hold-7")
        assert record.hold_record.exit_reason == "expired_escalated"


# ── Authorized Exit Tests ─────────────────────────────────────────────

class TestAuthorizedExit:
    """Test authorized exit from ESCALATED state with ExitReceipt."""

    def test_authorize_exit_approved(self):
        """authorize_exit(approve=True) transitions ESCALATED → RESERVED."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-exit-1", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-exit-1", tau_k_seconds=300)
        hm.escalate_hold("op-exit-1")
        result = hm.authorize_exit(
            "op-exit-1",
            authorized_by="operator@org",
            scope="refund-approval",
            authorization="ceremony-G-R5-001",
            reason="Evidence reviewed and approved",
            approve=True,
        )
        assert result["exited"] is True
        assert result["operation_state"] == OperationState.RESERVED.value
        assert result["exit_receipt"] is not None
        assert result["exit_receipt"]["authorized_by"] == "operator@org"
        assert result["exit_receipt"]["authorization"] == "ceremony-G-R5-001"
        assert result["exit_receipt"]["scope"] == "refund-approval"

    def test_authorize_exit_denied(self):
        """authorize_exit(approve=False) transitions ESCALATED → FAILED."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-exit-2", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-exit-2", tau_k_seconds=300)
        hm.escalate_hold("op-exit-2")
        result = hm.authorize_exit(
            "op-exit-2",
            authorized_by="operator@org",
            scope="refund-approval",
            authorization="ceremony-G-R5-001",
            reason="Evidence reviewed and denied",
            approve=False,
        )
        assert result["exited"] is True
        assert result["operation_state"] == OperationState.FAILED.value
        assert result["exit_receipt"] is not None
        assert result["exit_receipt"]["authorized_by"] == "operator@org"

    def test_authorize_exit_wrong_state(self):
        """authorize_exit() fails if operation is not in ESCALATED state."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-exit-3", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-exit-3", tau_k_seconds=300)
        result = hm.authorize_exit(
            "op-exit-3",
            authorized_by="operator@org",
            scope="test",
            authorization="ceremony-G-R5-001",
        )
        assert result["exited"] is False
        assert "not ESCALATED" in result["reason"]

    def test_exit_receipt_dataclass(self):
        """ExitReceipt captures who, scope, when, authorization, reason."""
        receipt = ExitReceipt(
            authorized_by="admin@org",
            scope="global-unlock",
            timestamp="2026-10-12T22:00:00Z",
            authorization="ceremony-G-R5-001",
            reason="Post-incident review complete",
        )
        d = receipt.to_dict()
        assert d["authorized_by"] == "admin@org"
        assert d["scope"] == "global-unlock"
        assert d["authorization"] == "ceremony-G-R5-001"
        assert d["reason"] == "Post-incident review complete"
        # Round-trip
        r2 = ExitReceipt.from_dict(d)
        assert r2.authorized_by == "admin@org"


# ── τ_K Temporal Validity Tests ──────────────────────────────────────

class TestTauKExpiry:
    """Test τ_K temporal validity bound checking."""

    def test_tau_k_not_expired(self):
        """τ_K has not expired when elapsed < τ_K."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-tau-1", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-tau-1", tau_k_seconds=3600)  # 1 hour
        # Check with current time — should not be expired
        result = hm.check_tau_k_expiry("op-tau-1")
        assert result["expired"] is False
        assert result["tau_k_remaining"] > 0

    def test_tau_k_expired(self):
        """τ_K has expired when elapsed > τ_K."""
        import time
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-tau-2", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-tau-2", tau_k_seconds=1)  # 1 second
        # Wait briefly to ensure time passes
        time.sleep(0.1)
        # Simulate expiry by checking with a future timestamp
        from datetime import datetime, timezone
        future = datetime.now(timezone.utc).timestamp() + 10
        result = hm.check_tau_k_expiry("op-tau-2", current_epoch=future)
        assert result["expired"] is True
        assert result["tau_k_remaining"] == 0.0

    def test_tau_k_no_bound_set(self):
        """check_tau_k_expiry returns not expired when no τ_K bound set."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-tau-3", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        hm.enter_hold("op-tau-3", tau_k_seconds=0.0, tau_k_source="explicitly unbounded test")
        result = hm.check_tau_k_expiry("op-tau-3")
        assert result["expired"] is False
        assert "No τ_K bound set" in result["reason"]

    def test_tau_k_check_on_non_hold_state(self):
        """check_tau_k_expiry returns not expired for non-HOLD operations."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(
            idempotency_key="op-tau-4", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg.register(rec)
        hm = HoldManager(reg)
        result = hm.check_tau_k_expiry("op-tau-4")
        assert result["expired"] is False
        assert "not in HOLD lifecycle" in result["reason"]


# ── Governor HOLD Lifecycle Integration Tests ─────────────────────────

class TestGovernorHoldLifecycle:
    """Test HOLD lifecycle methods on the governor.

    These tests use HoldManager.enter_hold() to put operations into HOLD state,
    since the governor's adjudicate() with neutral defaults typically produces
    FLAG (not HOLD). In production, HOLD is produced by the decision lattice
    when evidence is insufficient.
    """

    def test_governor_resolve_hold_with_evidence(self):
        """Governor.resolve_hold() re-adjudicates with new evidence."""
        gov, state = _setup_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        # Register the operation (CREATED state)
        gov.adjudicate(
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        # The operation is now in RESERVED (PASS/FLAG). To test HOLD lifecycle,
        # create a fresh operation and use HoldManager to enter HOLD.
        p2 = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(
            p2, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        # Use HoldManager to put p2 into HOLD from CREATED
        # First, we need a fresh CREATED operation
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-1", passage_id="p-test",
            agent_id="alice", action_type="payment", amount=10.0,
            resource_id="budget",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-1", tau_k_seconds=300)

        # Now resolve with better evidence (capacity must be in [0, 1])
        p3 = Passage(agent_id="alice", action_type="payment", amount=10.0,
                      idempotency_key="op-hold-test-1")
        r2 = gov.resolve_hold(
            "op-hold-test-1",
            passage=p3,
            pressure=0.1, velocity=0.1, capacity=0.9,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
            resource_id="budget", amount=10.0,
        )
        assert r2["hold_resolved"] is True
        assert r2["operation_state"] == OperationState.RESERVED.value

    def test_hard_veto_hold_does_not_release_another_operations_budget(self):
        gov, state = _setup_governor()
        assert state.resources["budget"].reserve(10.0)
        from tenir_governance.operations import OperationRecord
        key = "op-hold-no-reservation"
        rec = OperationRecord(
            idempotency_key=key, passage_id="p-hold-no-reservation",
            agent_id="alice", action_type="payment", amount=10.0,
            resource_id="budget",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold(key, tau_k_seconds=300)
        passage = Passage(
            agent_id="alice", action_type="payment", amount=10.0,
            idempotency_key=key,
        )
        result = gov.resolve_hold(
            key, passage=passage,
            pressure=0.2, velocity=0.2, capacity=0.9,
            authority=0.0, context_fit=0.9, evidence_quality=0.9,
            resource_id="budget", amount=10.0,
        )
        assert result["composed_decision"] == Decision.HARD_VETO.value
        assert state.resources["budget"].reserved == 10.0
        assert state.resources["budget"].consumed == 0.0

    def test_governor_resolve_hold_rejects_changed_amount(self):
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-identity", passage_id="p-hold-identity",
            agent_id="alice", action_type="payment", amount=10.0,
            resource_id="budget",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-identity", tau_k_seconds=300)
        passage = Passage(
            agent_id="alice", action_type="payment", amount=5.0,
            idempotency_key="op-hold-identity",
        )
        result = gov.resolve_hold(
            "op-hold-identity", passage=passage,
            pressure=0.1, velocity=0.1, capacity=0.9,
            resource_id="budget", amount=5.0,
        )
        assert result["hold_resolved"] is False
        assert "Amount differs" in result["reason"]
        assert gov.registry.get("op-hold-identity").state == OperationState.HOLD
        assert state.resources["budget"].reserved == 0.0

    def test_governor_resolve_hold_without_passage_is_rejected(self):
        """Omitting passage must not bypass fresh adjudication."""
        gov, _state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-no-passage", passage_id="p-no-passage",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-no-passage", tau_k_seconds=300)

        result = gov.resolve_hold("op-hold-no-passage")

        assert result["hold_resolved"] is False
        assert result["operation_state"] == OperationState.HOLD.value
        assert "Re-adjudication required" in result["reason"]
        assert gov.registry.get("op-hold-no-passage").state == OperationState.HOLD
        assert any(
            entry.idempotency_key == "op-hold-no-passage"
            and entry.kernel == "hold_resolution"
            for entry in gov.ledger.entries
        )

    def test_governor_escalate_hold(self):
        """Governor.escalate_hold() transitions HOLD → ESCALATED."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-2", passage_id="p-test",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-2", tau_k_seconds=300)
        result = gov.escalate_hold("op-hold-test-2", escalated_by="risk-officer")
        assert result["escalated"] is True
        assert result["operation_state"] == OperationState.ESCALATED.value

    def test_governor_authorize_exit(self):
        """Governor.authorize_exit() transitions ESCALATED → RESERVED."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-3", passage_id="p-test",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-3", tau_k_seconds=300)
        gov.escalate_hold("op-hold-test-3")
        result = gov.authorize_exit(
            "op-hold-test-3",
            authorized_by="operator@org",
            scope="refund-approval",
            authorization="ceremony-G-R5-001",
            approve=True,
        )
        assert result["exited"] is True
        assert result["operation_state"] == OperationState.RESERVED.value

    def test_governor_expire_hold(self, monkeypatch):
        """Governor.expire_hold() transitions HOLD → FAILED (fail-closed)."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-4", passage_id="p-test",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-4", tau_k_seconds=300)
        from tenir_governance.operations import hold as hold_module
        monkeypatch.setattr(hold_module, "_utc_now_seconds", lambda: time.time() + 301)
        result = gov.expire_hold("op-hold-test-4")
        assert result["expired"] is True
        assert result["operation_state"] == OperationState.FAILED.value

    def test_governor_expire_hold_refuses_before_tau_k(self):
        """A caller cannot force a HOLD exit before its explicit τ_K bound."""
        gov, _state = _setup_governor()
        rec = OperationRecord(
            idempotency_key="op-hold-too-early",
            passage_id="p-hold-too-early",
            agent_id="alice",
            action_type="payment",
        )
        assert gov.registry.register(rec)
        assert gov.hold_manager.enter_hold(
            "op-hold-too-early", tau_k_seconds=3600,
        )["entered"]

        result = gov.expire_hold("op-hold-too-early")

        assert result["expired"] is False
        latest = gov.registry.get("op-hold-too-early")
        assert latest.state == OperationState.HOLD
        assert latest.hold_record.exit_reason == ""

    def test_governor_resume_existing_hold(self):
        """resume_existing() returns resolve_or_escalate for HOLD operations."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-5", passage_id="p-test",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-5", tau_k_seconds=300)
        result = gov.resume_existing("op-hold-test-5")
        assert result["action"] == "resolve_or_escalate"
        assert result["operation_state"] == OperationState.HOLD.value

    def test_governor_resume_existing_escalated(self):
        """resume_existing() returns await_authorized_exit for ESCALATED."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-6", passage_id="p-test",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-6", tau_k_seconds=300)
        gov.escalate_hold("op-hold-test-6")
        result = gov.resume_existing("op-hold-test-6")
        assert result["action"] == "await_authorized_exit"
        assert result["operation_state"] == OperationState.ESCALATED.value

    def test_governor_find_holds(self):
        """find_holds() returns operations in HOLD state."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec1 = OperationRecord(
            idempotency_key="op-hold-test-7", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        rec2 = OperationRecord(
            idempotency_key="op-hold-test-8", passage_id="p2",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec1)
        gov.registry.register(rec2)
        gov.hold_manager.enter_hold("op-hold-test-7", tau_k_seconds=300)
        holds = gov.find_holds()
        assert len(holds) == 1
        assert holds[0].idempotency_key == "op-hold-test-7"

    def test_governor_recover_includes_holds(self):
        """recover() includes HOLD and ESCALATED operations."""
        gov, state = _setup_governor()
        from tenir_governance.operations import OperationRecord
        rec = OperationRecord(
            idempotency_key="op-hold-test-9", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        gov.registry.register(rec)
        gov.hold_manager.enter_hold("op-hold-test-9", tau_k_seconds=300)
        recovery = gov.recover()
        assert recovery["unresolved_count"] >= 1
        hold_entries = [r for r in recovery["unresolved"] if r["state"] == "hold"]
        assert len(hold_entries) >= 1
        assert hold_entries[0]["recommendation"] == "resolve_or_escalate"


# ── QUARANTINE_LOCK Tests ────────────────────────────────────────────

class TestQuarantineLock:
    """Test QUARANTINE_LOCK — persistent LEI-level guard."""

    def test_lock_quarantine(self):
        """lock_quarantine() activates the quarantine lock."""
        state = MembraneState()
        membrane = TransitionMembrane(state)
        membrane.lock_quarantine(locked_by="admin@org")
        assert membrane.quarantine_active is True
        assert membrane.quarantine_locked_by == "admin@org"
        assert membrane.quarantine_locked_at != ""

    def test_quarantine_blocks_admission(self):
        """When quarantine is active, I6 produces HARD_VETO."""
        state = MembraneState()
        state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                                    trusted_delegates=frozenset()))
        membrane = TransitionMembrane(state)
        membrane.lock_quarantine()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = membrane.adjudicate(passage=p)
        assert result["decision"] == Decision.HARD_VETO.value
        assert "I6" in result["rationale"]

    def test_release_quarantine_authenticated(self):
        """release_quarantine() requires authenticated authorization."""
        state = MembraneState()
        membrane = TransitionMembrane(state)
        membrane.lock_quarantine(locked_by="admin@org")
        result = membrane.release_quarantine(
            authorized_by="operator@org",
            scope="post-incident",
            authorization="ceremony-G-R5-001",
            reason="Incident resolved, safe to resume",
        )
        assert result["released"] is True
        assert membrane.quarantine_active is False
        assert result["receipt"] is not None
        assert result["receipt"]["authorized_by"] == "operator@org"
        assert result["receipt"]["authorization"] == "ceremony-G-R5-001"
        assert result["receipt"]["reason"] == "Incident resolved, safe to resume"

    def test_release_quarantine_not_locked(self):
        """release_quarantine() fails if no quarantine is active."""
        state = MembraneState()
        membrane = TransitionMembrane(state)
        result = membrane.release_quarantine(
            authorized_by="operator@org",
            scope="test",
            authorization="ceremony-G-R5-001",
        )
        assert result["released"] is False
        assert "No quarantine lock active" in result["reason"]

    def test_quarantine_release_receipt_preserved(self):
        """Quarantine release receipt is preserved after release."""
        state = MembraneState()
        membrane = TransitionMembrane(state)
        membrane.lock_quarantine(locked_by="admin@org")
        membrane.release_quarantine(
            authorized_by="operator@org",
            scope="post-incident",
            authorization="ceremony-G-R5-001",
            reason="Resolved",
        )
        assert membrane.quarantine_release_receipt is not None
        assert membrane.quarantine_release_receipt["authorized_by"] == "operator@org"

    def test_quarantine_does_not_block_after_release(self):
        """After release, admission is allowed again."""
        state = MembraneState()
        state.register_agent(Agent(agent_id="alice", authority_score=0.9,
                                    trusted_delegates=frozenset()))
        state.register_resource(Resource(resource_id="budget", capacity=100.0))
        membrane = TransitionMembrane(state)
        membrane.lock_quarantine()
        membrane.release_quarantine(
            authorized_by="operator@org",
            scope="post-incident",
            authorization="ceremony-G-R5-001",
        )
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = membrane.adjudicate(
            passage=p, resource_id="budget", amount=10.0,
        )
        assert result["decision"] != Decision.HARD_VETO.value or "I6" not in result["rationale"]


# ── Composition-Level HOLD Tests ─────────────────────────────────────

class TestCompositionHold:
    """Test composition-level HOLD coordination."""

    def _setup_composition(self):
        mgr = CompositionHoldManager(
            composition_id="liquidity-control-group",
            tau_k_composition_seconds=300,
        )
        mgr.register_tau("hedge-alpha-01", priority=1, tau_k_seconds=120)
        mgr.register_tau("hedge-beta-01", priority=2, tau_k_seconds=180)
        mgr.register_tau("hedge-gamma-01", priority=3, tau_k_seconds=90)
        return mgr

    def test_register_tau(self):
        """TAUs can be registered in the composition."""
        mgr = self._setup_composition()
        states = mgr.get_tau_states()
        assert "hedge-alpha-01" in states
        assert "hedge-beta-01" in states
        assert "hedge-gamma-01" in states
        assert states["hedge-alpha-01"]["priority"] == 1

    def test_enter_hold(self):
        """enter_hold() marks a TAU as held."""
        mgr = self._setup_composition()
        result = mgr.enter_hold("hedge-alpha-01", reason="LCR breach imminent")
        assert result["entered"] is True
        assert "hedge-alpha-01" in mgr.get_held_taus()

    def test_enter_hold_not_registered(self):
        """enter_hold() fails for unregistered TAUs."""
        mgr = self._setup_composition()
        result = mgr.enter_hold("unknown-tau")
        assert result["entered"] is False

    def test_priority_enforce(self):
        """Priority-Enforce: highest-priority TAU passes, others vetoed."""
        mgr = self._setup_composition()
        mgr.enter_hold("hedge-alpha-01", composition_invariant_violated=True)
        mgr.enter_hold("hedge-beta-01", composition_invariant_violated=True)
        result = mgr.resolve_composition_hold(
            CompositionHoldStrategy.PRIORITY_ENFORCE, conflict_type="C1",
        )
        assert result["strategy"] == "Priority-Enforce"
        assert "hedge-alpha-01" in result["released_taus"]
        assert "hedge-beta-01" in result["vetoed_taus"]

    def test_pareto_restore(self):
        """Pareto-Restore: hold minimum TAUs to restore invariant."""
        mgr = self._setup_composition()
        mgr.enter_hold("hedge-alpha-01", composition_invariant_violated=False)
        mgr.enter_hold("hedge-beta-01", composition_invariant_violated=True)
        mgr.enter_hold("hedge-gamma-01", composition_invariant_violated=True)
        result = mgr.resolve_composition_hold(
            CompositionHoldStrategy.PARETO_RESTORE, conflict_type="C1",
        )
        assert result["strategy"] == "Pareto-Restore"
        # TAUs that violated the invariant are held
        assert "hedge-beta-01" in result["held_taus"]
        assert "hedge-gamma-01" in result["held_taus"]
        # TAU that didn't violate is released
        assert "hedge-alpha-01" in result["released_taus"]

    def test_escalate(self):
        """Escalate: all TAUs held, escalated to Risk Officer."""
        mgr = self._setup_composition()
        mgr.enter_hold("hedge-alpha-01")
        result = mgr.resolve_composition_hold(
            CompositionHoldStrategy.ESCALATE, conflict_type="C1",
        )
        assert result["strategy"] == "Escalate"
        assert result["composition_hold"] is True
        assert len(result["held_taus"]) == 3
        assert "Risk Officer" in result["reason"]

    def test_fail_closed_collectif(self):
        """Fail-Closed Collectif: entire composition locked."""
        mgr = self._setup_composition()
        mgr.enter_hold("hedge-alpha-01")
        result = mgr.resolve_composition_hold(
            CompositionHoldStrategy.FAIL_CLOSED_COLLECTIF, conflict_type="C4",
        )
        assert result["strategy"] == "Fail-Closed-Collectif"
        assert result["composition_hold"] is True
        assert result.get("fail_closed") is True
        assert len(result["held_taus"]) == 3
        assert mgr.is_fail_closed is True

    def test_enter_fail_closed_directly(self):
        """enter_fail_closed() locks entire composition."""
        mgr = self._setup_composition()
        result = mgr.enter_fail_closed(reason="τ_K composition expired")
        assert result["fail_closed"] is True
        assert mgr.is_fail_closed is True
        assert len(result["held_taus"]) == 3

    def test_progressive_unblock(self):
        """Progressive unblock releases TAUs one at a time in priority order."""
        mgr = self._setup_composition()
        mgr.enter_fail_closed()
        mgr.enable_progressive_unblock()

        # Unblock first TAU (highest priority)
        r1 = mgr.progressive_unblock_next(invariant_verified=True)
        assert r1["unblocked"] is True
        assert r1["tau_id"] == "hedge-alpha-01"
        assert r1["remaining"] == 2

        # Unblock second TAU
        r2 = mgr.progressive_unblock_next(invariant_verified=True)
        assert r2["unblocked"] is True
        assert r2["tau_id"] == "hedge-beta-01"
        assert r2["remaining"] == 1

        # Unblock third TAU
        r3 = mgr.progressive_unblock_next(invariant_verified=True)
        assert r3["unblocked"] is True
        assert r3["tau_id"] == "hedge-gamma-01"
        assert r3["remaining"] == 0

        # All unblocked — fail-closed cleared
        assert mgr.is_fail_closed is False

    def test_progressive_unblock_invariant_not_verified(self):
        """Progressive unblock refuses when invariant not verified."""
        mgr = self._setup_composition()
        mgr.enter_fail_closed()
        mgr.enable_progressive_unblock()
        result = mgr.progressive_unblock_next(invariant_verified=False)
        assert result["unblocked"] is False
        assert "Invariant not verified" in result["reason"]

    def test_progressive_unblock_all_done(self):
        """Progressive unblock reports when all TAUs are unblocked."""
        mgr = self._setup_composition()
        mgr.enter_fail_closed()
        mgr.enable_progressive_unblock()
        for _ in range(3):
            mgr.progressive_unblock_next(invariant_verified=True)
        result = mgr.progressive_unblock_next(invariant_verified=True)
        assert result["unblocked"] is False
        assert "All TAUs unblocked" in result["reason"]

    def test_strategy_does_not_apply_to_conflict(self):
        """Strategy returns error when conflict type doesn't apply."""
        mgr = self._setup_composition()
        result = mgr.resolve_composition_hold(
            CompositionHoldStrategy.PARETO_RESTORE, conflict_type="C2",
        )
        # Pareto-Restore only applies to C1
        assert "does not apply" in result["reason"]


# ── State Machine Transition Tests ──────────────────────────────────

class TestHoldStateTransitions:
    """Test legal and illegal transitions involving HOLD and ESCALATED."""

    def test_created_to_hold(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        assert reg.transition_to("op-1", OperationState.HOLD)[0]

    def test_hold_to_reserved(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        assert reg.transition_to("op-1", OperationState.RESERVED)[0]

    def test_hold_to_failed(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        assert reg.transition_to("op-1", OperationState.FAILED)[0]

    def test_hold_to_escalated(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        assert reg.transition_to("op-1", OperationState.ESCALATED)[0]

    def test_escalated_to_reserved(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        reg.transition_to("op-1", OperationState.ESCALATED)
        assert reg.transition_to("op-1", OperationState.RESERVED)[0]

    def test_escalated_to_failed(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        reg.transition_to("op-1", OperationState.ESCALATED)
        assert reg.transition_to("op-1", OperationState.FAILED)[0]

    def test_escalated_to_unknown(self):
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        reg.transition_to("op-1", OperationState.ESCALATED)
        assert reg.transition_to("op-1", OperationState.UNKNOWN)[0]

    def test_illegal_hold_to_dispatching(self):
        """Cannot skip RESERVED — HOLD → DISPATCHING is illegal."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        assert not reg.transition_to("op-1", OperationState.DISPATCHING)[0]

    def test_illegal_escalated_to_dispatching(self):
        """Cannot skip RESERVED — ESCALATED → DISPATCHING is illegal."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        reg.transition_to("op-1", OperationState.HOLD)
        reg.transition_to("op-1", OperationState.ESCALATED)
        assert not reg.transition_to("op-1", OperationState.DISPATCHING)[0]

    def test_illegal_created_to_escalated(self):
        """Cannot skip HOLD — CREATED → ESCALATED is illegal."""
        reg = DurableOperationRegistry()
        rec = OperationRecord(idempotency_key="op-1", passage_id="p1",
                              agent_id="alice", action_type="payment")
        reg.register(rec)
        assert not reg.transition_to("op-1", OperationState.ESCALATED)[0]

    def test_hold_is_holding_property(self):
        """is_holding property returns True for HOLD and ESCALATED."""
        assert OperationState.HOLD.is_holding is True
        assert OperationState.ESCALATED.is_holding is True
        assert OperationState.CREATED.is_holding is False
        assert OperationState.RESERVED.is_holding is False

    def test_escalated_is_not_terminal(self):
        """ESCALATED is NOT terminal — it can transition to RESERVED/FAILED/UNKNOWN."""
        assert OperationState.ESCALATED.is_terminal is False

    def test_hold_may_not_have_been_dispatched(self):
        """HOLD and ESCALATED are NOT dispatched states."""
        assert OperationState.HOLD.may_have_been_dispatched is False
        assert OperationState.ESCALATED.may_have_been_dispatched is False


# ── Persistence Tests ────────────────────────────────────────────────

class TestHoldPersistence:
    """Test that HOLD state and HoldRecord survive registry restart."""

    def test_hold_record_survives_restart(self, tmp_path):
        store = str(tmp_path / "operations.json")
        reg1 = DurableOperationRegistry(store_path=store)
        rec = OperationRecord(
            idempotency_key="op-persist-1", passage_id="p1",
            agent_id="alice", action_type="payment", amount=25.0,
        )
        reg1.register(rec)
        hm = HoldManager(reg1)
        hm.enter_hold("op-persist-1", tau_k_seconds=300, tau_k_source="on-call SLA")

        # Restart while still held: persistence must not depend on bypassing
        # the governor's fresh re-adjudication path.
        reg2 = DurableOperationRegistry(store_path=store)
        loaded = reg2.get("op-persist-1")
        assert loaded is not None
        assert loaded.state == OperationState.HOLD
        assert loaded.hold_record is not None
        assert loaded.hold_record.tau_k_seconds == 300
        assert loaded.hold_record.tau_k_source == "on-call SLA"
        assert loaded.hold_record.exit_reason == ""

    def test_exit_receipt_survives_restart(self, tmp_path):
        store = str(tmp_path / "operations.json")
        reg1 = DurableOperationRegistry(store_path=store)
        rec = OperationRecord(
            idempotency_key="op-persist-2", passage_id="p1",
            agent_id="alice", action_type="payment",
        )
        reg1.register(rec)
        hm = HoldManager(reg1)
        hm.enter_hold("op-persist-2", tau_k_seconds=300)
        hm.escalate_hold("op-persist-2", escalated_by="risk-officer")

        # Authorized exit (transitions ESCALATED → RESERVED, persists receipt)
        hm.authorize_exit(
            "op-persist-2",
            authorized_by="operator@org",
            scope="post-incident",
            authorization="ceremony-G-R5-001",
            reason="Approved after review",
            approve=True,
        )

        # Restart
        reg2 = DurableOperationRegistry(store_path=store)
        loaded = reg2.get("op-persist-2")
        assert loaded is not None
        assert loaded.hold_record is not None
        assert loaded.hold_record.exit_receipt is not None
        assert loaded.hold_record.exit_receipt.authorized_by == "operator@org"
        assert loaded.hold_record.exit_receipt.authorization == "ceremony-G-R5-001"
        assert loaded.hold_record.escalated_by == "risk-officer"
