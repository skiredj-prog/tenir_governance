# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Tests for the composition layer, governor, and schema adapter.

Adapted from the prototype's test_composition.py to use TenirKernel
(via ScoringKernelAdapter) instead of the prototype's ScoringKernel.

Bridge mapping: ScoringKernel → ScoringKernelAdapter(TenirKernel)
The adapter injects constraint geometry coordinates and extra gate reasons
(tau floor, option space) that TenirKernel doesn't natively evaluate.
"""
import threading
import pytest
from tenir_kernel.kernel import TenirKernel, KernelPolicy
from tenir_governance.composition import (
    DualKernelGovernor, ScoringKernelAdapter,
    HashChainedLedger, compose,
    conform_to_schema, POSTURE_MAP, EXECUTION_DISPOSITION_MAP,
)
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.objects import Agent, Passage, Resource, Delegation
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord, OperationState


class TestCompose:
    """Decision lattice composition."""

    def test_both_pass(self):
        d, r = compose(Decision.PASS.value, Decision.PASS.value)
        assert d == Decision.PASS.value

    def test_scoring_veto(self):
        d, r = compose(Decision.HARD_VETO.value, Decision.PASS.value)
        assert d == Decision.HARD_VETO.value
        assert "scoring" in r

    def test_membrane_veto(self):
        d, r = compose(Decision.PASS.value, Decision.HARD_VETO.value)
        assert d == Decision.HARD_VETO.value
        assert "membrane" in r

    def test_both_veto(self):
        d, r = compose(Decision.HARD_VETO.value, Decision.HARD_VETO.value)
        assert d == Decision.HARD_VETO.value

    def test_scoring_flag(self):
        d, r = compose(Decision.FLAG.value, Decision.PASS.value)
        assert d == Decision.FLAG.value

    def test_membrane_flag(self):
        d, r = compose(Decision.PASS.value, Decision.FLAG.value)
        assert d == Decision.FLAG.value

    def test_membrane_hold(self):
        d, r = compose(Decision.PASS.value, Decision.HOLD.value)
        assert d == Decision.HOLD.value
        assert "hold" in r.lower()

    def test_veto_overrides_hold(self):
        d, r = compose(Decision.HARD_VETO.value, Decision.HOLD.value)
        assert d == Decision.HARD_VETO.value

    def test_veto_overrides_flag(self):
        d, r = compose(Decision.FLAG.value, Decision.HARD_VETO.value)
        assert d == Decision.HARD_VETO.value

    def test_flag_plus_near_limit_budget_escalates_to_hold(self):
        """Per review §2: FLAG + near-limit budget → HOLD, not just FLAG."""
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=0.90, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.HOLD.value
        assert "escalated to HOLD" in r

    def test_flag_plus_low_budget_stays_flag(self):
        """FLAG with low budget utilization stays FLAG."""
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=0.50, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.FLAG.value

    def test_pass_plus_near_limit_budget_stays_pass(self):
        """PASS with near-limit budget stays PASS (no escalation for PASS)."""
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.PASS.value, Decision.PASS.value,
            budget_utilization=0.90, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.PASS.value

    def test_scoring_none_fail_closed_hold(self):
        d, r = compose(None, Decision.PASS.value)
        assert d == Decision.HOLD.value

    def test_membrane_none_fail_closed_hold(self):
        d, r = compose(Decision.PASS.value, None)
        assert d == Decision.HOLD.value

    def test_both_none_fail_closed_hold(self):
        d, r = compose(None, None)
        assert d == Decision.HOLD.value

    def test_scoring_none_with_membrane_veto_still_veto(self):
        d, r = compose(None, Decision.HARD_VETO.value)
        assert d == Decision.HARD_VETO.value

    def test_flag_with_indeterminate_budget_fail_closed(self):
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=None, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.HOLD.value

    def test_flag_without_budget_in_scope_stays_flag(self):
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=None, budget_in_scope=False,
        )
        assert d == Decision.FLAG.value

    def test_veto_retains_hold_reason(self):
        d, r = compose(Decision.HARD_VETO.value, Decision.HOLD.value)
        assert d == Decision.HARD_VETO.value
        assert "retained" in r

    def test_threshold_just_below(self):
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=0.849, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.FLAG.value

    def test_threshold_exactly_at(self):
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=0.85, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.HOLD.value

    def test_threshold_just_above(self):
        policy = CompositionPolicy(budget_escalation_threshold=0.85)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=0.851, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.HOLD.value

    def test_custom_threshold(self):
        policy = CompositionPolicy(budget_escalation_threshold=0.50)
        d, r = compose(
            Decision.FLAG.value, Decision.PASS.value,
            budget_utilization=0.60, budget_in_scope=True,
            policy=policy,
        )
        assert d == Decision.HOLD.value


class TestLedger:
    """Hash-chained ledger."""

    def test_chain_integrity(self):
        ledger = HashChainedLedger()
        ledger.append("scoring", "PASS", "ok")
        ledger.append("membrane", "PASS", "ok")
        ledger.append("composed", "PASS", "both passed")
        assert ledger.verify_chain() is True
        assert ledger.entry_count == 3

    def test_tamper_detection(self):
        ledger = HashChainedLedger()
        ledger.append("scoring", "PASS", "ok")
        ledger.append("membrane", "PASS", "ok")
        # Tamper
        ledger._entries[0].decision = "HARD_VETO"
        assert ledger.verify_chain() is False

    def test_genesis_link(self):
        ledger = HashChainedLedger()
        ledger.append("scoring", "PASS", "ok")
        assert ledger._entries[0].previous_hash == "GENESIS"
        assert ledger.verify_chain() is True

    def test_find_by_idempotency_key(self):
        ledger = HashChainedLedger()
        ledger.append("scoring", "PASS", "ok", idempotency_key="key-1")
        ledger.append("membrane", "PASS", "ok", idempotency_key="key-1")
        ledger.append("scoring", "PASS", "ok", idempotency_key="key-2")
        entries = ledger.find_by_idempotency_key("key-1")
        assert len(entries) == 2


class TestDualKernelGovernor:
    """End-to-end two-kernel composition with TenirKernel bridge."""

    def _adjudicate(self, gov, passage, **kwargs):
        """Supply explicit measured epistemic inputs for ordinary integration cases."""
        kwargs.setdefault("authority", 0.9)
        kwargs.setdefault("context_fit", 1.0)
        kwargs.setdefault("evidence_quality", 1.0)
        return gov.adjudicate(passage, **kwargs)

    def _setup(self, budget_capacity=100.0, policy=None):
        state = MembraneState()
        budget = Resource(resource_id="budget", capacity=budget_capacity)
        state.register_resource(budget)
        alice = Agent(agent_id="alice", authority_score=0.9,
                      trusted_delegates=frozenset({"bob"}))
        state.register_agent(alice)
        # Bridge: TenirKernel via ScoringKernelAdapter
        tenir_kernel = TenirKernel()
        scoring = ScoringKernelAdapter(tenir_kernel)
        membrane = TransitionMembrane(state)
        governor = DualKernelGovernor(scoring, membrane, policy=policy)
        return governor, state

    def test_registry_budget_flags_roll_back_when_persistence_fails(self, tmp_path, monkeypatch):
        store = tmp_path / "operations.json"
        registry = DurableOperationRegistry(str(store))
        record = OperationRecord(
            idempotency_key="registry-write-failure",
            passage_id="passage-registry-write-failure",
            agent_id="alice",
            action_type="payment",
            amount=10.0,
            resource_id="budget",
            state=OperationState.CREATED,
        )
        assert registry.register(record)

        def fail_persist():
            raise OSError("simulated registry disk failure")

        monkeypatch.setattr(registry, "_persist", fail_persist)
        with pytest.raises(OSError, match="simulated registry disk failure"):
            registry.set_budget_flags("registry-write-failure", budget_reserved=True)

        # Both the live record and the durable copy remain at the old value.
        assert registry._records["registry-write-failure"].budget_reserved is False
        reloaded = DurableOperationRegistry(str(store))
        current = reloaded.get("registry-write-failure")
        assert current is not None
        assert current.budget_reserved is False

    def test_registry_post_rename_error_keeps_durable_budget_ownership(self, tmp_path, monkeypatch):
        store = tmp_path / "operations.json"
        registry = DurableOperationRegistry(str(store))
        record = OperationRecord(
            idempotency_key="registry-post-rename",
            passage_id="passage-registry-post-rename",
            agent_id="alice",
            action_type="payment",
            amount=10.0,
            resource_id="budget",
            state=OperationState.CREATED,
        )
        assert registry.register(record)
        persist = registry._persist

        def persist_then_raise():
            persist()
            raise OSError("simulated directory fsync failure after rename")

        monkeypatch.setattr(registry, "_persist", persist_then_raise)
        # The file already contains the new ownership flag. Report committed
        # rather than causing the caller to release a reservation still owned
        # by the durable operation record.
        assert registry.set_budget_flags(
            "registry-post-rename", budget_reserved=True,
        )
        reloaded = DurableOperationRegistry(str(store))
        assert reloaded.get("registry-post-rename").budget_reserved is True

    @pytest.mark.parametrize("failure_mode", ["false", "exception"])
    def test_failed_budget_flag_persistence_compensates_reservation(
        self, monkeypatch, failure_mode
    ):
        gov, state = self._setup()
        record = OperationRecord(
            idempotency_key="budget-flag-failure",
            passage_id="passage-budget-flag-failure",
            agent_id="alice",
            action_type="payment",
            amount=10.0,
            resource_id="budget",
            state=OperationState.CREATED,
        )
        assert gov.registry.register(record)

        def fail_flag_write(*_args, **_kwargs):
            if failure_mode == "exception":
                raise OSError("simulated registry persistence failure")
            return False

        monkeypatch.setattr(gov.registry, "set_budget_flags", fail_flag_write)
        assert not gov._reserve_budget("budget-flag-failure", "budget", 10.0)
        assert state.resources["budget"].reserved == 0.0
        current = gov.registry.get("budget-flag-failure")
        assert current is not None
        assert current.budget_reserved is False

    def test_quarantine_activated_after_membrane_check_blocks_admission(self, monkeypatch):
        gov, state = self._setup()
        original = gov.membrane.adjudicate

        def adjudicate_then_quarantine(**kwargs):
            result = original(**kwargs)
            state.lock_quarantine("race-test")
            return result

        monkeypatch.setattr(gov.membrane, "adjudicate", adjudicate_then_quarantine)
        passage = Passage(
            agent_id="alice", action_type="payment", amount=10.0,
            idempotency_key="quarantine-admission-race",
        )
        result = self._adjudicate(gov, 
            passage, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["composed_decision"] == Decision.HARD_VETO.value
        assert state.resources["budget"].reserved == 0.0
        assert result["operation_state"] != OperationState.RESERVED.value

    def test_quarantine_race_blocks_admission_without_budget_scope(self, monkeypatch):
        gov, state = self._setup()
        original = gov.membrane.adjudicate

        def adjudicate_then_quarantine(**kwargs):
            result = original(**kwargs)
            state.lock_quarantine("unbudgeted-race-test")
            return result

        monkeypatch.setattr(gov.membrane, "adjudicate", adjudicate_then_quarantine)
        passage = Passage(
            agent_id="alice", action_type="read_only",
            idempotency_key="quarantine-unbudgeted-race",
        )
        result = self._adjudicate(gov, 
            passage, pressure=0.2, velocity=0.3, capacity=1.0,
        )
        assert result["composed_decision"] == Decision.HARD_VETO.value
        assert result["operation_state"] != OperationState.RESERVED.value

    def test_resource_reregistration_cannot_reset_budget_or_lock(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["operation_state"] == OperationState.RESERVED.value
        original = state.resources["budget"]
        original_lock = state.get_lock("budget")
        with pytest.raises(ValueError, match="replacement is prohibited"):
            state.register_resource(Resource(resource_id="budget", capacity=1000.0))
        assert state.resources["budget"] is original
        assert state.get_lock("budget") is original_lock
        assert original.reserved == 10.0

    def test_active_provider_call_does_not_block_quarantine_or_reconciliation(self):
        from types import SimpleNamespace
        from tenir_governance.operations.provider import ProviderOutcome

        started = threading.Event()
        finish = threading.Event()

        class BlockingProvider:
            def execute(self, **_kwargs):
                started.set()
                assert finish.wait(3), "test provider was not released"
                return SimpleNamespace(
                    outcome=ProviderOutcome.CONFIRMED,
                    provider_reference="provider-1",
                    latency_ms=1,
                    error=None,
                )

            def reconcile(self, **_kwargs):
                raise AssertionError("reconcile must not race active execute")

        gov, state = self._setup()
        gov.provider = BlockingProvider()
        passage = Passage(
            agent_id="alice", action_type="payment", amount=10.0,
            idempotency_key="active-provider-race",
        )
        result = self._adjudicate(gov, 
            passage, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["operation_state"] == OperationState.RESERVED.value

        worker = threading.Thread(
            target=lambda: gov.dispatch_and_execute(
                "active-provider-race", "budget", 10.0,
            ),
            daemon=True,
        )
        worker.start()
        assert started.wait(2), "provider.execute did not start"

        quarantine_done = threading.Event()
        locker = threading.Thread(
            target=lambda: (state.lock_quarantine("test"), quarantine_done.set()),
            daemon=True,
        )
        locker.start()
        assert quarantine_done.wait(1), (
            "provider I/O must not hold the global quarantine lock"
        )
        assert state.quarantine_active is True

        reconciliation = gov.reconcile_unknown("active-provider-race")
        assert reconciliation["reconciled"] is False
        assert "still active" in reconciliation["reason"]

        finish.set()
        worker.join(2)
        assert not worker.is_alive()

    def test_dispatch_blocks_provider_when_budget_flag_commit_fails(self, monkeypatch):
        gov, state = self._setup()
        passage = Passage(
            agent_id="alice", action_type="payment", amount=10.0,
            idempotency_key="dispatch-flag-write-failure",
        )
        result = self._adjudicate(gov, 
            passage, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["operation_state"] == OperationState.RESERVED.value
        resource = state.resources["budget"]

        def fail_flags(*_args, **_kwargs):
            raise OSError("simulated flag persistence failure")

        monkeypatch.setattr(gov.registry, "set_budget_flags", fail_flags)

        class FailIfExecutedProvider:
            def execute(self, **_kwargs):
                pytest.fail("provider must not execute when budget ownership persistence fails")

        gov.provider = FailIfExecutedProvider()
        dispatched = gov.dispatch_and_execute(
            "dispatch-flag-write-failure", "budget", 10.0,
        )
        assert dispatched["dispatched"] is False
        assert "provider execution blocked" in dispatched["reason"]
        assert resource.reserved == 10.0
        assert resource.consumed == 0.0
        current = gov.registry.get("dispatch-flag-write-failure")
        assert current.budget_reserved is True
        assert current.budget_committed is False

    def test_resolve_hold_missing_fresh_epistemic_inputs_fails_closed(self):
        """HOLD re-adjudication must not inherit favorable epistemic defaults."""
        gov, _state = self._setup()
        key = "hold-missing-epistemic-evidence"
        record = OperationRecord(
            idempotency_key=key,
            passage_id=f"passage-{key}",
            agent_id="alice",
            action_type="read_only",
            amount=0.0,
            resource_id="",
            state=OperationState.HOLD,
        )
        assert gov.registry.register(record)
        passage = Passage(
            agent_id="alice",
            action_type="read_only",
            idempotency_key=key,
        )

        result = gov.resolve_hold(key, passage=passage)

        assert result["scoring_decision"] == Decision.HARD_VETO.value
        assert result["composed_decision"] == Decision.HARD_VETO.value
        # Missing evidence must veto and terminally close the HOLD.
        # "hold_resolved" means safely terminal, not admitted for execution.
        assert result["hold_resolved"] is True
        assert result["operation_state"] == OperationState.FAILED.value
        assert "authority missing" in result["scoring_rationale"]
        assert "context_fit missing" in result["scoring_rationale"]
        assert "evidence_quality missing" in result["scoring_rationale"]

    def test_resolve_hold_without_passage_does_not_re_adjudicate(self):
        """Omitting the passage must leave the operation in HOLD."""
        gov, _state = self._setup()
        key = "hold-no-passage"
        record = OperationRecord(
            idempotency_key=key,
            passage_id=f"passage-{key}",
            agent_id="alice",
            action_type="read_only",
            amount=0.0,
            resource_id="",
            state=OperationState.HOLD,
        )
        assert gov.registry.register(record)

        result = gov.resolve_hold(key)

        assert result["hold_resolved"] is False
        assert result["operation_state"] == OperationState.HOLD.value
        assert "Re-adjudication required" in result["reason"]
        assert gov.registry.get(key).state == OperationState.HOLD

    def test_governor_missing_epistemic_inputs_fail_closed(self):
        gov, _state = self._setup()
        passage = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = gov.adjudicate(
            passage, pressure=0.2, velocity=0.3, capacity=1.0,
        )
        assert result["scoring_decision"] == Decision.HARD_VETO.value
        assert result["composed_decision"] == Decision.HARD_VETO.value
        assert "authority missing" in result["scoring_rationale"]
        assert "context_fit missing" in result["scoring_rationale"]
        assert "evidence_quality missing" in result["scoring_rationale"]

    def test_safe_payment_both_pass(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert result["ledger_valid"] is True
        assert result["ledger_entries"] == 3

    def test_scoring_catches_stress_membrane_passes(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.95, velocity=0.95, capacity=0.3,
            resource_id="budget", amount=10.0,
        )
        assert result["scoring_decision"] == Decision.HARD_VETO.value
        assert result["composed_decision"] == Decision.HARD_VETO.value

    def test_membrane_catches_bad_delegation_scoring_passes(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="delegation", recipient_id="eve")
        delegation = Delegation(delegating_agent_id="alice", delegatee_agent_id="eve")
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            delegation=delegation,
        )
        assert result["scoring_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert result["membrane_decision"] == Decision.HARD_VETO.value
        assert result["composed_decision"] == Decision.HARD_VETO.value
        assert "I7" in result["membrane_rationale"]

    def test_membrane_catches_budget_scoring_passes(self):
        gov, state = self._setup(budget_capacity=50.0)
        p1 = Passage(agent_id="alice", action_type="payment", amount=30.0)
        r1 = self._adjudicate(gov, 
            p1, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=30.0,
        )
        assert r1["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        gov.membrane.commit_passage(p1, resource_id="budget", amount=30.0)

        p2 = Passage(agent_id="alice", action_type="payment", amount=30.0)
        r2 = self._adjudicate(gov, 
            p2, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=30.0,
        )
        assert r2["scoring_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert r2["membrane_decision"] == Decision.HARD_VETO.value
        assert r2["composed_decision"] == Decision.HARD_VETO.value

    def test_both_kernel_flag_compose_to_flag(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.9, velocity=0.9, capacity=0.7,
            resource_id="budget", amount=10.0,
        )
        assert result["scoring_decision"] == Decision.FLAG.value
        assert result["composed_decision"] == Decision.FLAG.value

    def test_ledger_records_both_kernels(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        entries = gov.ledger.entries
        assert len(entries) == 3
        assert entries[0].kernel == "scoring"
        assert entries[1].kernel == "membrane"
        assert entries[2].kernel == "composed"
        assert gov.ledger.verify_chain() is True

    def test_mandatory_check_not_skipped_by_healthy_score(self):
        """Per review §1: even with a perfect score, mandatory checks must run."""
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="delegation", recipient_id="eve")
        delegation = Delegation(delegating_agent_id="alice", delegatee_agent_id="eve")
        result = self._adjudicate(gov, 
            p, pressure=0.01, velocity=0.01, capacity=1.0,
            delegation=delegation,
        )
        assert result["scoring_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert result["membrane_decision"] == Decision.HARD_VETO.value
        assert result["composed_decision"] == Decision.HARD_VETO.value

    def test_flag_near_limit_budget_escalates_to_hold(self):
        """Per review §2: FLAG from scoring + near-limit budget → HOLD."""
        gov, state = self._setup(budget_capacity=100.0)
        p1 = Passage(agent_id="alice", action_type="payment", amount=80.0)
        r1 = self._adjudicate(gov, 
            p1, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=80.0,
        )
        assert r1["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        gov.membrane.commit_passage(p1, resource_id="budget", amount=80.0)

        p2 = Passage(agent_id="alice", action_type="payment", amount=5.0)
        r2 = self._adjudicate(gov, 
            p2, pressure=0.9, velocity=0.9, capacity=0.7,
            resource_id="budget", amount=5.0,
        )
        assert r2["scoring_decision"] == Decision.FLAG.value
        assert r2["membrane_decision"] == Decision.PASS.value
        assert r2["composed_decision"] == Decision.HOLD.value

    def test_execution_status_separated_from_decision(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert "execution_status" in result
        assert result["execution_status"] == "pending"
        assert result["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)

    def test_idempotency_key_auto_generated(self):
        """Per review 2 §4: passage auto-generates an idempotency key."""
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        assert p.idempotency_key is not None
        assert p.idempotency_key.startswith("op-")

    def test_duplicate_dispatch_blocked(self):
        """Per review 2 §4: a confirmed effect cannot be dispatched again."""
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        r1 = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert r1["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)

        # Same idempotency key — should be blocked
        p2 = Passage(agent_id="alice", action_type="payment", amount=10.0,
                     idempotency_key=p.idempotency_key)
        r2 = self._adjudicate(gov, 
            p2, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert r2["composed_decision"] == Decision.HARD_VETO.value
        assert "Duplicate" in r2["composed_rationale"]

    def test_kernel_exception_fail_closed(self):
        """Per review 2 §2: kernel exception → HOLD, never PASS."""
        gov, state = self._setup()

        # Create a broken scoring kernel that throws
        class BrokenScoring:
            def adjudicate(self, **kwargs):
                raise RuntimeError("Database connection lost")

        gov.scoring = BrokenScoring()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        # Must be HOLD, not PASS
        assert result["composed_decision"] == Decision.HOLD.value
        assert result["scoring_decision"] is None

    def test_custom_policy_threshold(self):
        """Custom policy threshold affects escalation."""
        gov, state = self._setup(budget_capacity=100.0, policy=CompositionPolicy(budget_escalation_threshold=0.50))
        p1 = Passage(agent_id="alice", action_type="payment", amount=45.0)
        r1 = self._adjudicate(gov, 
            p1, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=45.0,
        )
        assert r1["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        gov.membrane.commit_passage(p1, resource_id="budget", amount=45.0)

        # FLAG + 50% budget with 50% threshold → HOLD
        p2 = Passage(agent_id="alice", action_type="payment", amount=5.0)
        r2 = self._adjudicate(gov, 
            p2, pressure=0.9, velocity=0.9, capacity=0.7,
            resource_id="budget", amount=5.0,
        )
        assert r2["scoring_decision"] == Decision.FLAG.value
        assert r2["composed_decision"] == Decision.HOLD.value

    def test_dispatch_and_confirm(self):
        """Full dispatch lifecycle: adjudicate → dispatch → confirm."""
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        r1 = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert r1["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert r1["operation_state"] == OperationState.RESERVED.value

        # Dispatch
        r2 = gov.dispatch(p.idempotency_key, resource_id="budget", amount=10.0)
        assert r2["dispatched"] is True
        assert r2["operation_state"] == OperationState.DISPATCHING.value

        # Confirm
        assert gov.confirm(p.idempotency_key) is True
        assert gov.registry.get(p.idempotency_key).state == OperationState.CONFIRMED

    def test_fail_releases_budget(self):
        """Fail after reserve releases the budget reservation."""
        gov, state = self._setup(budget_capacity=100.0)
        p = Passage(agent_id="alice", action_type="payment", amount=30.0)
        r1 = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=30.0,
        )
        assert r1["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)

        # Fail the operation
        assert gov.fail(p.idempotency_key, resource_id="budget", amount=30.0) is True
        resource = state.resources.get("budget")
        assert resource.reserved == 0.0

    def test_resume_existing_not_found(self):
        """resume_existing returns not_found for unknown keys."""
        gov, state = self._setup()
        result = gov.resume_existing("nonexistent-key")
        assert result["action"] == "not_found"

    def test_resume_existing_reserved(self):
        """resume_existing returns dispatch for RESERVED operations."""
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        result = gov.resume_existing(p.idempotency_key)
        assert result["action"] == "dispatch"
        assert result["operation_state"] == OperationState.RESERVED.value

    def test_recover_finds_unresolved(self):
        """recover() finds DISPATCHING and UNKNOWN operations."""
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        gov.dispatch(p.idempotency_key, resource_id="budget", amount=10.0)

        recovery = gov.recover()
        assert recovery["unresolved_count"] == 1
        assert recovery["unresolved"][0]["recommendation"] == "reconcile"


    @pytest.mark.parametrize("amount", [-1.0, float("nan"), float("inf"), float("-inf")])
    def test_invalid_amount_fails_closed_before_reservation(self, amount):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=amount,
        )
        assert result["composed_decision"] == Decision.HARD_VETO.value
        assert result["execution_status"] == "blocked"
        assert gov.registry.get(p.idempotency_key) is None
        assert state.resources["budget"].reserved == 0.0

    def test_fail_does_not_release_unowned_budget(self):
        gov, state = self._setup()
        assert state.resources["budget"].reserve(10.0)
        from tenir_governance.operations import OperationRecord
        key = "fail-without-budget-ownership"
        record = OperationRecord(
            idempotency_key=key, passage_id="p-fail-unowned",
            agent_id="alice", action_type="payment", amount=10.0,
            resource_id="budget",
        )
        assert gov.registry.register(record)
        assert gov.fail(key, resource_id="budget", amount=10.0)
        assert gov.registry.get(key).state == OperationState.FAILED
        assert state.resources["budget"].reserved == 10.0
        assert state.resources["budget"].consumed == 0.0

    def test_dispatch_rejects_amount_mismatch_without_committing(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["operation_state"] == OperationState.RESERVED.value
        dispatched = gov.dispatch(p.idempotency_key, resource_id="budget", amount=5.0)
        assert dispatched["dispatched"] is False
        assert "does not match" in dispatched["reason"]
        assert gov.registry.get(p.idempotency_key).state == OperationState.RESERVED
        assert state.resources["budget"].reserved == 10.0
        assert state.resources["budget"].consumed == 0.0

    def test_quarantine_blocks_previously_reserved_dispatch(self):
        gov, state = self._setup()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        result = self._adjudicate(gov, 
            p, pressure=0.2, velocity=0.3, capacity=1.0,
            resource_id="budget", amount=10.0,
        )
        assert result["operation_state"] == OperationState.RESERVED.value
        state.lock_quarantine("test")
        dispatched = gov.dispatch(p.idempotency_key, resource_id="budget", amount=10.0)
        assert dispatched["dispatched"] is False
        assert "QUARANTINE_LOCK" in dispatched["reason"]
        assert gov.registry.get(p.idempotency_key).state == OperationState.RESERVED
        assert state.resources["budget"].reserved == 10.0
        assert state.resources["budget"].consumed == 0.0


class TestScoringKernelAdapter:
    """Tests for the TenirKernel bridge adapter."""

    def _setup(self):
        tk = TenirKernel()
        return ScoringKernelAdapter(tk)

    def test_safe_inputs_pass(self):
        adapter = self._setup()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=1.0, context_fit=1.0, evidence_quality=1.0,
        )
        assert result["decision"] in (Decision.PASS.value, Decision.FLAG.value)
        assert result["s_score"] is not None

    def test_missing_mandatory_epistemic_inputs_fail_closed(self):
        adapter = self._setup()
        result = adapter.adjudicate(pressure=0.2, velocity=0.3, capacity=1.0)
        assert result["decision"] == Decision.HARD_VETO.value
        assert "authority missing" in result["rationale"]
        assert "context_fit missing" in result["rationale"]
        assert "evidence_quality missing" in result["rationale"]

    def test_high_stress_hard_veto(self):
        adapter = self._setup()
        result = adapter.adjudicate(pressure=0.95, velocity=0.95, capacity=0.3)
        assert result["decision"] == Decision.HARD_VETO.value

    def test_moderate_stress_flag(self):
        adapter = self._setup()
        result = adapter.adjudicate(
            pressure=0.9, velocity=0.9, capacity=0.7,
            authority=1.0, context_fit=1.0, evidence_quality=1.0,
        )
        assert result["decision"] == Decision.FLAG.value

    def test_low_evidence_quality_flag(self):
        adapter = self._setup()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=1.0, context_fit=1.0, evidence_quality=0.3
        )
        assert result["decision"] == Decision.FLAG.value

    def test_irreversibility_exceeds_authority_hard_veto(self):
        adapter = self._setup()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            irreversibility_materiality=0.8, authority=0.5,
            context_fit=1.0, evidence_quality=1.0
        )
        assert result["decision"] == Decision.HARD_VETO.value

    def test_constraint_geometry_returned(self):
        adapter = self._setup()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=1.0, context_fit=1.0, evidence_quality=1.0,
        )
        assert "constraint_geometry" in result
        assert result["constraint_geometry"] is not None
        assert result["constraint_geometry"]["geometry_id"] == "tenir-2.0-reference"

    def test_returns_kernel_identifier(self):
        adapter = self._setup()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=1.0, context_fit=1.0, evidence_quality=1.0,
        )
        assert result["kernel"] == "scoring"


class TestSchemaAdapter:
    """Tests for the TENIR 2.0 schema 1.2 conformance adapter."""

    def test_missing_canonical_context_fails_closed(self):
        with pytest.raises(ValueError, match="canonical_context is required"):
            conform_to_schema({"composed_decision": "PASS"})

    def _make_result(self, composed="PASS", scoring="PASS", membrane="PASS"):
        # Explicit test evidence/context: production callers must supply real
        # values rather than relying on the adapter to fabricate them.
        canonical_context = {
            "rubric_version": "test-fixture-1",
            "timestamp_utc": "2026-10-19T00:00:00Z",
            "identity": {
                "unit_name": "test-unit",
                "entity_class": "organization",
                "declared_scale": "team",
            },
            "tau": {
                "tau_hypothesis": "Preserve accountable human authority.",
                "tau_integrity": "intact",
            },
            "epistemic": {
                "status": "ok",
                "evidence_quality": 1.0,
                "manipulation_risk": 0.0,
                "drift": 0.0,
                "epistemic_surrender_risk": 0.0,
                "confidence_ceiling": "high",
            },
            "scores": {
                "pressure": 0.2,
                "velocity": 0.3,
                "capacity": 1.0,
                "option_space": 0.8,
                "stability": 1.0,
                "cooperative_control_score": 1.0,
                "trajectory_risk_score": 0.0,
                "irreversible_pathway_materiality": 0.0,
            },
            "collapse_flags": [],
            "active_profiles": ["agentic-governance-profile-v1.0.0"],
            "suppressed_profiles": [],
            "profile_conflicts": [],
            "narrative_distortion_summary": {},
            "irreversible_pathway_assessment": {
                "materially_affects_irreversible_pathway": False,
                "raw_data_human_verification_required": False,
                "human_verified_raw_data": False,
            },
            "assumptions": [],
            "constraint_geometry": {
                "geometry_id": "tenir-2.0-reference",
                "coordinates": {
                    "authority": 1.0,
                    "context_fit": 1.0,
                    "authority_source": "explicit test evidence",
                    "context_source": "explicit test evidence",
                },
            },
            "trace": {"trace_id": "fixture-trace", "stages": []},
            "synthesis": {
                "structural_core": {
                    "pressure": 0.2,
                    "velocity": 0.3,
                    "capacity": 1.0,
                    "option_space": 0.8,
                    "tau_integrity": "intact",
                    "stability": 1.0,
                },
                "epistemic_state": {"status": "ok"},
                "final_posture": "NORMAL",
                "rationale": ["Explicit test fixture."],
                "degraded_inputs": [],
            },
            "schema_version": "1.2",
            "degraded_mode": ["sparse_ingress"] if composed in ("HOLD", "HARD_VETO") else ["none"],
        }
        return {
            "canonical_context": canonical_context,
            "composed_decision": composed,
            "scoring_decision": scoring,
            "scoring_rationale": "test",
            "scoring_s": 1.0,
            "membrane_decision": membrane,
            "membrane_rationale": "test",
            "membrane_checks": [],
            "composed_rationale": "test",
            "execution_status": "pending",
            "operation_state": "reserved",
            "idempotency_key": "op-test",
            "passage_id": "p1",
            "ledger_entries": 3,
            "ledger_valid": True,
        }

    def test_pass_maps_to_normal_posture(self):
        result = conform_to_schema(self._make_result(composed="PASS"))
        assert result["posture"] == "NORMAL"
        assert result["execution_disposition"] == "allow"
        assert result["execution_allowed"] is True
        assert result["degraded_mode"] == ["none"]

    def test_flag_maps_to_caution_posture(self):
        result = conform_to_schema(self._make_result(composed="FLAG"))
        assert result["posture"] == "CAUTION"
        assert result["execution_disposition"] == "allow_with_alert"
        assert result["execution_allowed"] is True
        assert result["degraded_mode"] == ["none"]

    def test_hold_maps_to_holding_first_posture(self):
        result = conform_to_schema(self._make_result(composed="HOLD"))
        assert result["posture"] == "HOLDING-FIRST"
        assert result["execution_disposition"] == "hold"
        assert result["execution_allowed"] is False
        assert result["degraded_mode"] == ["sparse_ingress"]

    def test_hard_veto_maps_to_holding_first_posture(self):
        result = conform_to_schema(self._make_result(composed="HARD_VETO"))
        assert result["posture"] == "HOLDING-FIRST"
        assert result["execution_disposition"] == "block"
        assert result["execution_allowed"] is False
        assert result["degraded_mode"] == ["sparse_ingress"]

    def test_posture_and_execution_mode_separate(self):
        """Per KB3: posture and execution mode are separate fields."""
        result = conform_to_schema(self._make_result(composed="PASS"))
        assert "posture" in result
        assert "execution_mode" in result
        assert result["execution_mode"] == "enforce"
        assert result["posture"] != result["execution_mode"]

    def test_compliance_class_visible(self):
        """Per KB3: compliance class must stay visible."""
        result = conform_to_schema(self._make_result())
        assert result["compliance_class"] == "profile-bound"

    def test_degraded_mode_visible(self):
        """Per KB3: degraded status must stay visible."""
        result = conform_to_schema(self._make_result(composed="HOLD"))
        assert result["degraded_mode"] == ["sparse_ingress"]

    def test_disclosure_metadata_present(self):
        """Per versioning policy §6: disclosure metadata required."""
        result = conform_to_schema(self._make_result())
        assert "disclosure" in result
        d = result["disclosure"]
        assert d["doctrinal_version"] == "TENIR 2.0"
        assert d["platform_version"] == "5.6.1"
        assert d["contract_version"] == "1.2"
        assert d["compliance_class"] == "profile-bound"
        assert "agentic-governance-profile-v1.0.0" in d["active_profiles"]
        assert "canonical-doctrine" in d["semantic_authority_not_held"]

    def test_synthesis_structure(self):
        result = conform_to_schema(self._make_result())
        assert "synthesis" in result
        s = result["synthesis"]
        assert s["final_posture"] == "NORMAL"
        assert "structural_core" in s
        assert "agency_state" in s
        assert "trajectory_state" in s
        assert "principal_failure_mode" in s

    def test_trace_structure(self):
        result = conform_to_schema(self._make_result())
        assert "trace" in result
        t = result["trace"]
        assert t["trace_id"] == "op-test"
        assert t["passage_id"] == "p1"
        assert len(t["stages"]) >= 3
        assert t["ledger_entries"] == 3
        assert t["ledger_valid"] is True

    def test_principal_failure_mode_for_hold(self):
        result = conform_to_schema(self._make_result(
            composed="HOLD", scoring=None, membrane="HOLD"
        ))
        assert result["synthesis"]["principal_failure_mode"] is not None

    def test_principal_failure_mode_none_for_pass(self):
        result = conform_to_schema(self._make_result(composed="PASS"))
        assert result["synthesis"]["principal_failure_mode"] is None

    def test_missing_irreversible_assessment_fails_closed(self):
        source = self._make_result()
        source["canonical_context"].pop("irreversible_pathway_assessment")
        with pytest.raises(ValueError, match="irreversible_pathway_assessment"):
            conform_to_schema(source)

    def test_preserves_explicit_irreversible_pathway_assessment(self):
        source = self._make_result()
        source["irreversible_pathway_assessment"] = {
            "materially_affects_irreversible_pathway": True,
            "raw_data_human_verification_required": True,
            "human_verified_raw_data": False,
        }
        result = conform_to_schema(source)
        assert result["irreversible_pathway_assessment"] == source["irreversible_pathway_assessment"]

    def test_schema_version(self):
        result = conform_to_schema(self._make_result())
        assert result["schema_version"] == "1.2"
        assert result["engine_spec_version"] == "TENIR-2.0"


# Import OperationState for the dispatch tests
from tenir_governance.operations.registry import OperationState
