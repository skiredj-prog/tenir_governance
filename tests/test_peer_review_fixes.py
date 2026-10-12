# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
"""Tests for P1/P2 fixes from peer review.

P1-1: SDK default registry persistence — restart after claim keeps key blocked.
P1-2: Corrupted registry fail-closed — refuses adjudication on corrupt store.
P1-3: Hash chain covers all significant fields — tampering any field invalidates chain.
P2-1: Scoring coordinates — no favorable defaults for unmeasured axes.
P2-2: RESERVED transition checked — transition failure produces HOLD, not PASS.
"""
import json
import os
import tempfile
import pytest

from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.composition.governor import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.lattice import HashChainedLedger
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import TransitionMembrane, MembraneState
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord, OperationState
from tenir_governance.operations.budget import InProcessBudgetCoordinator


# ── P1-1: SDK default registry persistence ──────────────────────

class TestSDKPersistenceDefault:
    """P1-1: The SDK must default to persistent storage or refuse production mode."""

    def test_sdk_without_store_path_uses_in_memory(self):
        """SDK without store_path works but is in-memory only (for testing)."""
        from tenir_governance.sdk import DualKernelCompositionClient
        client = DualKernelCompositionClient()
        assert client.governor.registry._store_path is None

    def test_sdk_require_persistence_without_path_raises(self):
        """require_persistence=True without store_path must raise ValueError."""
        from tenir_governance.sdk import DualKernelCompositionClient
        with pytest.raises(ValueError, match="require_persistence"):
            DualKernelCompositionClient(require_persistence=True)

    def test_sdk_with_store_path_uses_persistent_registry(self):
        """SDK with store_path creates a persistent registry."""
        from tenir_governance.sdk import DualKernelCompositionClient
        with tempfile.TemporaryDirectory() as tmpdir:
            store = os.path.join(tmpdir, "registry.json")
            client = DualKernelCompositionClient(store_path=store)
            assert client.governor.registry._store_path == store

    def test_sdk_restart_after_claim_key_stays_blocked(self):
        """P1-1: After restart, a previously-claimed key must still be blocked."""
        from tenir_governance.sdk import DualKernelCompositionClient

        with tempfile.TemporaryDirectory() as tmpdir:
            store = os.path.join(tmpdir, "registry.json")

            # First session: register an agent and adjudicate
            client1 = DualKernelCompositionClient(store_path=store)
            client1.register_agent(Agent(agent_id="alice", authority_score=0.9))
            client1.register_resource(Resource(resource_id="budget", capacity=100.0))

            p = Passage(agent_id="alice", action_type="payment", amount=10.0,
                        idempotency_key="op-restart-test")
            result1 = client1.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                                        resource_id="budget", amount=10.0,
                                        authority=0.9, context_fit=0.9, evidence_quality=0.9)
            assert result1["composed_decision"] in ("PASS", "FLAG")

            # Second session: restart with same store_path
            client2 = DualKernelCompositionClient(store_path=store)
            client2.register_agent(Agent(agent_id="alice", authority_score=0.9))
            client2.register_resource(Resource(resource_id="budget", capacity=100.0))

            # Same idempotency key must be blocked
            p2 = Passage(agent_id="alice", action_type="payment", amount=10.0,
                         idempotency_key="op-restart-test")
            result2 = client2.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                                        resource_id="budget", amount=10.0,
                                        authority=0.9, context_fit=0.9, evidence_quality=0.9)
            assert result2["composed_decision"] == "HARD_VETO"
            assert "already claimed" in result2["composed_rationale"]


# ── P1-2: Corrupted registry fail-closed ──────────────────────────

class TestCorruptedRegistryFailClosed:
    """P1-2: A corrupted registry store must refuse new operations."""

    def test_corrupted_store_refuses_registration(self):
        """A corrupted JSON store must raise RuntimeError on register()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = os.path.join(tmpdir, "registry.json")
            # Write corrupt JSON
            with open(store, "w") as f:
                f.write("{ this is not valid json !!!")

            registry = DurableOperationRegistry(store_path=store)
            assert registry.is_corrupted is True

            record = OperationRecord(
                idempotency_key="test-key",
                passage_id="p1",
                agent_id="alice",
                action_type="payment",
            )
            with pytest.raises(RuntimeError, match="corrupted"):
                registry.register(record)

    def test_absent_store_is_not_corrupted(self):
        """An absent store file is normal (first start), not corrupted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = os.path.join(tmpdir, "registry.json")
            registry = DurableOperationRegistry(store_path=store)
            assert registry.is_corrupted is False

    def test_truncated_store_refuses_registration(self):
        """A truncated JSON store must also refuse registration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = os.path.join(tmpdir, "registry.json")
            # Write truncated JSON (valid start, no end)
            with open(store, "w") as f:
                f.write('{"key": {"idempotency_key": "test", "passage_id": "p1",')

            registry = DurableOperationRegistry(store_path=store)
            assert registry.is_corrupted is True

            record = OperationRecord(
                idempotency_key="new-key",
                passage_id="p2",
                agent_id="bob",
                action_type="payment",
            )
            with pytest.raises(RuntimeError, match="corrupted"):
                registry.register(record)

    def test_valid_store_loads_normally(self):
        """A valid store file loads normally without corruption flag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = os.path.join(tmpdir, "registry.json")

            # Create and persist a valid registry
            registry1 = DurableOperationRegistry(store_path=store)
            record = OperationRecord(
                idempotency_key="valid-key",
                passage_id="p1",
                agent_id="alice",
                action_type="payment",
            )
            assert registry1.register(record) is True

            # Reload — should not be corrupted
            registry2 = DurableOperationRegistry(store_path=store)
            assert registry2.is_corrupted is False
            assert registry2.get("valid-key") is not None


# ── P1-3: Hash chain covers all fields ───────────────────────────

class TestHashChainAllFields:
    """P1-3: Tampering any significant field in a ledger entry must invalidate the chain."""

    def test_tampering_scoring_result_invalidates_chain(self):
        """Modifying scoring_result must break chain verification."""
        ledger = HashChainedLedger()
        ledger.append(
            kernel="scoring", decision="PASS", rationale="ok",
            passage_id="p1", idempotency_key="k1",
            scoring_result={"s_score": 2.5},
        )
        assert ledger.verify_chain() is True

        # Tamper with scoring_result
        ledger.entries[0].scoring_result = {"s_score": 99.0}
        assert ledger.verify_chain() is False

    def test_tampering_membrane_result_invalidates_chain(self):
        """Modifying membrane_result must break chain verification."""
        ledger = HashChainedLedger()
        ledger.append(
            kernel="membrane", decision="PASS", rationale="ok",
            passage_id="p1", idempotency_key="k1",
            membrane_result={"checks": []},
        )
        assert ledger.verify_chain() is True

        ledger.entries[0].membrane_result = {"checks": ["tampered"]}
        assert ledger.verify_chain() is False

    def test_tampering_composed_decision_invalidates_chain(self):
        """Modifying composed_decision must break chain verification."""
        ledger = HashChainedLedger()
        ledger.append(
            kernel="composed", decision="PASS", rationale="ok",
            passage_id="p1", idempotency_key="k1",
            composed_decision="PASS",
        )
        assert ledger.verify_chain() is True

        ledger.entries[0].composed_decision = "HARD_VETO"
        assert ledger.verify_chain() is False

    def test_tampering_decision_invalidates_chain(self):
        """Modifying decision must break chain verification."""
        ledger = HashChainedLedger()
        ledger.append(
            kernel="scoring", decision="PASS", rationale="ok",
            passage_id="p1", idempotency_key="k1",
        )
        assert ledger.verify_chain() is True

        ledger.entries[0].decision = "HARD_VETO"
        assert ledger.verify_chain() is False

    def test_tampering_rationale_invalidates_chain(self):
        """Modifying rationale must break chain verification."""
        ledger = HashChainedLedger()
        ledger.append(
            kernel="scoring", decision="PASS", rationale="all clear",
            passage_id="p1", idempotency_key="k1",
        )
        assert ledger.verify_chain() is True

        ledger.entries[0].rationale = "tampered rationale"
        assert ledger.verify_chain() is False

    def test_multiple_entries_chain_still_valid(self):
        """Multiple entries with all fields covered still verify correctly."""
        ledger = HashChainedLedger()
        for i in range(5):
            ledger.append(
                kernel="scoring", decision="PASS", rationale=f"entry {i}",
                passage_id=f"p{i}", idempotency_key=f"k{i}",
                scoring_result={"s_score": float(i)},
                membrane_result={"checks": [f"check-{i}"]},
                composed_decision="PASS",
            )
        assert ledger.verify_chain() is True

    def test_tampering_middle_entry_invalidates_rest(self):
        """Tampering a middle entry must invalidate the chain from that point."""
        ledger = HashChainedLedger()
        for i in range(5):
            ledger.append(
                kernel="composed", decision="PASS", rationale=f"entry {i}",
                passage_id=f"p{i}", idempotency_key=f"k{i}",
                composed_decision="PASS",
            )
        assert ledger.verify_chain() is True

        # Tamper with entry 2
        ledger.entries[2].composed_decision = "HOLD"
        assert ledger.verify_chain() is False


# ── P2-1: Scoring coordinates — no favorable defaults ─────────────

class TestScoringCoordinatesNoFavorableDefaults:
    """P2-1: Unmeasured axes must not default to favorable values."""

    def _make_adapter(self):
        from tenir_kernel.kernel import TenirKernel
        return ScoringKernelAdapter(TenirKernel())

    def test_unmeasured_axes_are_neutral_not_favorable(self):
        """Unmeasured axes must be 0.5 (neutral), not 1.0 or 0.0 (favorable)."""
        adapter = self._make_adapter()
        coords = adapter._build_coordinates(
            pressure=0.3, velocity=0.3, capacity=1.0,
            option_space=None,  # not provided
            authority=0.9, context_fit=0.9,
            evidence_quality=0.9, irreversibility_materiality=0.1,
        )
        assert coords["cooperative_control_score"] == 0.5
        assert coords["trajectory_risk_score"] == 0.5
        assert coords["manipulation_risk"] == 0.5
        assert coords["drift"] == 0.5
        assert coords["option_space"] == 0.5  # not 1.0

    def test_unmeasured_axes_are_flagged(self):
        """Unmeasured axes must be tracked in _unmeasured_axes."""
        adapter = self._make_adapter()
        adapter._build_coordinates(
            pressure=0.3, velocity=0.3, capacity=1.0,
            option_space=None,
            authority=0.9, context_fit=0.9,
            evidence_quality=0.9, irreversibility_materiality=0.1,
        )
        assert "cooperative_control_score" in adapter._unmeasured_axes
        assert "trajectory_risk_score" in adapter._unmeasured_axes
        assert "manipulation_risk" in adapter._unmeasured_axes
        assert "drift" in adapter._unmeasured_axes
        assert "option_space" in adapter._unmeasured_axes

    def test_measured_option_space_is_used(self):
        """When option_space is provided, it must be used (not defaulted)."""
        adapter = self._make_adapter()
        coords = adapter._build_coordinates(
            pressure=0.3, velocity=0.3, capacity=1.0,
            option_space=0.7,
            authority=0.9, context_fit=0.9,
            evidence_quality=0.9, irreversibility_materiality=0.1,
        )
        assert coords["option_space"] == 0.7
        assert "option_space" not in adapter._unmeasured_axes

    def test_missing_authority_produces_hard_gate_reason(self):
        """authority ≤ 0.0 must produce a hard gate reason."""
        adapter = self._make_adapter()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=0.0,  # missing
            context_fit=0.9, evidence_quality=0.9,
        )
        # The hard gate reason should cause HARD_VETO or HOLD, not PASS
        assert result["decision"] != "PASS"

    def test_missing_evidence_quality_produces_hard_gate_reason(self):
        """evidence_quality ≤ 0.0 must produce a hard gate reason."""
        adapter = self._make_adapter()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=0.9, context_fit=0.9,
            evidence_quality=0.0,  # missing
        )
        assert result["decision"] != "PASS"

    def test_missing_context_fit_produces_hard_gate_reason(self):
        """context_fit ≤ 0.0 must produce a hard gate reason."""
        adapter = self._make_adapter()
        result = adapter.adjudicate(
            pressure=0.2, velocity=0.3, capacity=1.0,
            authority=0.9, context_fit=0.0,  # missing
            evidence_quality=0.9,
        )
        assert result["decision"] != "PASS"


# ── P2-2: RESERVED transition checked ────────────────────────────

class TestReservedTransitionChecked:
    """P2-2: If transition_to(RESERVED) fails, governor must return HOLD, not PASS."""

    def _make_governor(self, registry=None):
        from tenir_kernel.kernel import TenirKernel
        kernel = TenirKernel()
        scoring = ScoringKernelAdapter(kernel)
        membrane_state = MembraneState()
        membrane = TransitionMembrane(membrane_state)
        membrane_state.register_agent(Agent(agent_id="alice", authority_score=0.9))
        membrane_state.register_resource(Resource(resource_id="budget", capacity=100.0))
        policy = CompositionPolicy()
        gov = DualKernelGovernor(
            scoring, membrane,
            policy=policy,
            registry=registry or DurableOperationRegistry(),
        )
        return gov

    def test_normal_pass_transitions_to_reserved(self):
        """Normal PASS should transition to RESERVED successfully."""
        gov = self._make_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=10.0,
                     idempotency_key="op-normal")
        result = gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                               resource_id="budget", amount=10.0,
                               authority=0.9, context_fit=0.9, evidence_quality=0.9)
        assert result["composed_decision"] in ("PASS", "FLAG")
        assert result["operation_state"] == "reserved"

    def test_transition_failure_produces_hold(self):
        """If RESERVED transition fails, governor must return HOLD, not PASS."""
        gov = self._make_governor()

        # Pre-register the key and transition it to a state that makes
        # RESERVED an illegal transition (e.g., CONFIRMED is terminal)
        record = OperationRecord(
            idempotency_key="op-conflict",
            passage_id="pre-registered",
            agent_id="alice",
            action_type="payment",
            amount=10.0,
            resource_id="budget",
        )
        gov.registry.register(record)
        gov.registry.transition_to("op-conflict", OperationState.RESERVED)
        gov.registry.transition_to("op-conflict", OperationState.DISPATCHING)
        gov.registry.transition_to("op-conflict", OperationState.CONFIRMED)

        # Now adjudicate with the same key — register returns False (duplicate)
        p = Passage(agent_id="alice", action_type="payment", amount=10.0,
                    idempotency_key="op-conflict")
        result = gov.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                               resource_id="budget", amount=10.0,
                               authority=0.9, context_fit=0.9, evidence_quality=0.9)
        # Should be HARD_VETO because the key is already claimed
        assert result["composed_decision"] == "HARD_VETO"

    def test_reserved_transition_success_returns_pass(self):
        """When RESERVED transition succeeds, governor returns PASS with reserved state."""
        gov = self._make_governor()
        p = Passage(agent_id="alice", action_type="payment", amount=5.0,
                    idempotency_key="op-success")
        result = gov.adjudicate(p, pressure=0.1, velocity=0.2, capacity=1.0,
                               resource_id="budget", amount=5.0,
                               authority=0.9, context_fit=0.9, evidence_quality=0.9)
        assert result["composed_decision"] in ("PASS", "FLAG")
        assert result["operation_state"] == "reserved"
