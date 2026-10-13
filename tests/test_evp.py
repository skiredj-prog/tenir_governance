"""Comprehensive tests for EVP (Intended Value Preservation).

Per doctrine (TENIR Property-Preserving Transitions Manuscript, Layer 4):
- EVP measures intended value protection as an OUTCOME of governance discipline.
- EVP ∉ Admissibility Function. Admissibility precedes value measurement.
- High expected value never justifies an otherwise inadmissible transition.
- EVP Gross / EVP Net, friction costs, Governance Debt are consequences,
  not inputs to D.
- EVP and τ⁻¹ don't imply each other.

P1-3 fix: Frozen equations — definition and calculation must agree.
    EVP Gross    = intended_value × confidence
    EVP Net      = realized_value − execution_loss
    EVP Ratio    = EVP Net / EVP Gross
    Friction     = hold_delay + escalation + reconciliation
                   + denied_opportunity + processing
    Friction is reported separately, NOT subtracted from EVP Net.
"""
import pytest
from tenir_governance.assessment.evp import (
    EVPAssessor,
    EVPReport,
    EVPGross,
    EVPNet,
    FrictionCost,
    GovernanceDebt,
)


class TestEVPGross:
    """Test EVP Gross — the total intended value of the operation."""

    def test_expected_value_is_intended_times_confidence(self):
        gross = EVPGross(intended_value=1000.0, confidence=0.8)
        assert gross.expected_value == 800.0

    def test_full_confidence(self):
        gross = EVPGross(intended_value=500.0, confidence=1.0)
        assert gross.expected_value == 500.0

    def test_zero_confidence(self):
        gross = EVPGross(intended_value=1000.0, confidence=0.0)
        assert gross.expected_value == 0.0

    def test_to_dict(self):
        gross = EVPGross(intended_value=100.0, confidence=0.5, description="test")
        d = gross.to_dict()
        assert d["intended_value"] == 100.0
        assert d["confidence"] == 0.5
        assert d["expected_value"] == 50.0
        assert d["description"] == "test"


class TestEVPNet:
    """Test EVP Net — actual value realized minus execution losses.

    Frozen equation: EVP Net = realized_value − execution_loss
    Friction is NOT subtracted from EVP Net.
    """

    def test_net_value_is_realized_minus_loss(self):
        net = EVPNet(realized_value=950.0, execution_loss=50.0)
        assert net.net_value == 900.0

    def test_zero_loss(self):
        net = EVPNet(realized_value=1000.0, execution_loss=0.0)
        assert net.net_value == 1000.0

    def test_total_loss(self):
        net = EVPNet(realized_value=0.0, execution_loss=1000.0)
        assert net.net_value == -1000.0

    def test_to_dict(self):
        net = EVPNet(realized_value=800.0, execution_loss=100.0)
        d = net.to_dict()
        assert d["realized_value"] == 800.0
        assert d["execution_loss"] == 100.0
        assert d["net_value"] == 700.0


class TestFrictionCost:
    """Test friction costs — overhead imposed by governance discipline.

    Friction is reported SEPARATELY, not subtracted from EVP Net.
    """

    def test_total_is_sum_of_all_components(self):
        fc = FrictionCost(
            hold_delay_cost=10.0,
            escalation_overhead=5.0,
            reconciliation_effort=3.0,
            denied_opportunity_cost=20.0,
            processing_overhead=1.0,
        )
        assert fc.total == 39.0

    def test_empty_friction_is_zero(self):
        fc = FrictionCost()
        assert fc.total == 0.0

    def test_to_dict_contains_all_components(self):
        fc = FrictionCost(hold_delay_cost=1.0, processing_overhead=0.5)
        d = fc.to_dict()
        assert d["hold_delay_cost"] == 1.0
        assert d["escalation_overhead"] == 0.0
        assert d["reconciliation_effort"] == 0.0
        assert d["denied_opportunity_cost"] == 0.0
        assert d["processing_overhead"] == 0.5
        assert d["total"] == 1.5


class TestGovernanceDebt:
    """Test governance debt — accumulated cost of unresolved actions."""

    def test_total_debt_is_locked_budget_plus_delay_cost(self):
        gd = GovernanceDebt(locked_budget=500.0, accumulated_delay_cost=100.0)
        assert gd.total_debt == 600.0

    def test_total_unresolved_counts_all_types(self):
        gd = GovernanceDebt(
            unresolved_hold_count=3,
            unresolved_escalated_count=2,
            unresolved_unknown_count=1,
        )
        assert gd.total_unresolved == 6

    def test_empty_debt_is_zero(self):
        gd = GovernanceDebt()
        assert gd.total_debt == 0.0
        assert gd.total_unresolved == 0

    def test_to_dict(self):
        gd = GovernanceDebt(
            unresolved_hold_count=2,
            locked_budget=300.0,
            accumulated_delay_cost=50.0,
        )
        d = gd.to_dict()
        assert d["unresolved_hold_count"] == 2
        assert d["total_unresolved"] == 2
        assert d["locked_budget"] == 300.0
        assert d["total_debt"] == 350.0


class TestEVPReport:
    """Test the complete EVP report.

    Frozen equations:
        EVP Gross    = intended_value × confidence
        EVP Net      = realized_value − execution_loss
        EVP Ratio    = EVP Net / EVP Gross
        Friction     = reported separately, NOT subtracted from EVP Net
    """

    def test_evp_ratio_when_gross_is_zero(self):
        report = EVPReport(
            gross=EVPGross(intended_value=0.0, confidence=1.0),
            net=EVPNet(realized_value=0.0),
        )
        assert report.evp_ratio == 0.0

    def test_evp_ratio_perfect_realization(self):
        report = EVPReport(
            gross=EVPGross(intended_value=1000.0, confidence=1.0),
            net=EVPNet(realized_value=1000.0, execution_loss=0.0),
        )
        assert report.evp_ratio == 1.0

    def test_evp_ratio_partial_realization(self):
        report = EVPReport(
            gross=EVPGross(intended_value=1000.0, confidence=1.0),
            net=EVPNet(realized_value=800.0, execution_loss=100.0),
        )
        # net = 700, gross expected = 1000, ratio = 0.7
        assert report.evp_ratio == pytest.approx(0.7)

    def test_evp_ratio_does_not_include_friction(self):
        """P1-3: Friction is NOT subtracted from EVP Net."""
        report = EVPReport(
            gross=EVPGross(intended_value=1000.0, confidence=1.0),
            net=EVPNet(realized_value=1000.0, execution_loss=0.0),
            friction=FrictionCost(hold_delay_cost=100.0, processing_overhead=5.0),
        )
        # Even with significant friction, ratio is 1.0 because friction
        # is reported separately, not subtracted from EVP Net
        assert report.evp_ratio == 1.0

    def test_is_input_to_admissibility_is_always_false(self):
        """Doctrine constraint: EVP is NEVER an input to admissibility."""
        report = EVPReport()
        assert report.is_input_to_admissibility is False

    def test_to_dict(self):
        report = EVPReport(
            gross=EVPGross(intended_value=1000.0, confidence=1.0),
            net=EVPNet(realized_value=900.0),
            friction=FrictionCost(processing_overhead=1.0),
        )
        d = report.to_dict()
        assert d["is_input_to_admissibility"] is False
        assert "gross" in d
        assert "net" in d
        assert "friction" in d
        assert "evp_ratio" in d


class TestEVPAssessor:
    """Test the EVP assessor."""

    def test_assess_confirmed_operation(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=950.0,
            confidence=1.0,
        )
        assert report.gross.intended_value == 1000.0
        assert report.gross.expected_value == 1000.0
        assert report.net.realized_value == 950.0
        assert report.net.net_value == 950.0
        assert report.is_input_to_admissibility is False

    def test_assess_failed_operation(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="failed",
            intended_value=1000.0,
            denied_opportunity_value=1000.0,
        )
        # Failed → realized_value defaults to 0
        assert report.net.realized_value == 0.0
        assert report.net.net_value == 0.0
        assert report.friction.denied_opportunity_cost == 1000.0
        assert report.evp_ratio == 0.0

    def test_assess_with_confidence(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=800.0,
            confidence=0.8,
        )
        # gross expected = 1000 * 0.8 = 800
        # net = 800
        # ratio = 800 / 800 = 1.0
        assert report.gross.expected_value == 800.0
        assert report.evp_ratio == pytest.approx(1.0)

    def test_assess_with_hold_duration(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            hold_duration_seconds=300.0,
        )
        # hold_delay_cost = 300 * 0.001 = 0.3
        assert report.friction.hold_delay_cost == pytest.approx(0.3)

    def test_assess_with_escalation(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            escalated_duration_seconds=60.0,
        )
        # escalation_overhead = 60 * 0.01 = 0.6
        assert report.friction.escalation_overhead == pytest.approx(0.6)

    def test_assess_with_reconciliation(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            reconciliation_count=3,
        )
        # reconciliation_effort = 3 * 5.0 = 15.0
        assert report.friction.reconciliation_effort == 15.0

    def test_assess_with_execution_loss(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            execution_loss=100.0,
        )
        assert report.net.execution_loss == 100.0
        assert report.net.net_value == 900.0

    def test_assess_with_custom_friction_config(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            hold_duration_seconds=100.0,
            friction_config={"hold_delay_rate": 0.01},
        )
        # hold_delay_cost = 100 * 0.01 = 1.0
        assert report.friction.hold_delay_cost == 1.0

    def test_assess_with_governance_debt(self):
        assessor = EVPAssessor()
        gd = GovernanceDebt(
            unresolved_hold_count=5,
            locked_budget=2000.0,
        )
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            governance_debt=gd,
        )
        assert report.governance_debt.unresolved_hold_count == 5
        assert report.governance_debt.locked_budget == 2000.0

    def test_assess_governance_debt_snapshot(self):
        assessor = EVPAssessor()
        gd = assessor.assess_governance_debt(
            hold_count=3,
            escalated_count=1,
            unknown_count=2,
            locked_budget=5000.0,
            accumulated_delay_cost=200.0,
        )
        assert gd.unresolved_hold_count == 3
        assert gd.unresolved_escalated_count == 1
        assert gd.unresolved_unknown_count == 2
        assert gd.total_unresolved == 6
        assert gd.total_debt == 5200.0

    def test_rationale_for_confirmed(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=900.0,
        )
        assert "confirmed" in report.rationale.lower()

    def test_rationale_for_failed(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="failed",
            intended_value=1000.0,
        )
        assert "failed" in report.rationale.lower()


class TestP1HandCalculatedEVP:
    """P1-3: Hand-calculated EVP tests verifying the frozen equations.

    These tests verify that the definition, formula, ratio, and examples
    all agree. Every value is hand-calculated, not derived from the code.
    """

    def test_confirmed_full_value_no_friction(self):
        """Confirmed operation, full value realized, no friction.

        EVP Gross = 1000 × 1.0 = 1000
        EVP Net   = 1000 − 0 = 1000
        EVP Ratio = 1000 / 1000 = 1.0
        Friction  = 0 (no hold, no escalation, no reconciliation)
                    + 0 (no denied opportunity) + 0.5 (processing overhead)
                  = 0.5
        """
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
            confidence=1.0,
        )
        assert report.gross.expected_value == 1000.0
        assert report.net.net_value == 1000.0
        assert report.evp_ratio == 1.0
        # Friction is only processing_overhead (0.5)
        assert report.friction.total == pytest.approx(0.5)

    def test_confirmed_partial_value_with_execution_loss(self):
        """Confirmed operation with execution loss.

        EVP Gross = 2000 × 0.9 = 1800
        EVP Net   = 1700 − 200 = 1500
        EVP Ratio = 1500 / 1800 = 0.833...
        Friction  = 0 (no hold) + 0 + 0 + 0 + 0.5 = 0.5
        """
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=2000.0,
            realized_value=1700.0,
            confidence=0.9,
            execution_loss=200.0,
        )
        assert report.gross.expected_value == 1800.0
        assert report.net.net_value == 1500.0
        assert report.evp_ratio == pytest.approx(1500.0 / 1800.0)
        assert report.friction.total == pytest.approx(0.5)

    def test_failed_operation_with_denied_opportunity(self):
        """Failed operation, value denied.

        EVP Gross = 500 × 1.0 = 500
        EVP Net   = 0 − 0 = 0 (failed → realized defaults to 0)
        EVP Ratio = 0 / 500 = 0.0
        Friction  = 0 + 0 + 0 + 500 (denied opportunity) + 0.5 = 500.5
        """
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="failed",
            intended_value=500.0,
            denied_opportunity_value=500.0,
        )
        assert report.gross.expected_value == 500.0
        assert report.net.net_value == 0.0
        assert report.evp_ratio == 0.0
        assert report.friction.denied_opportunity_cost == 500.0
        assert report.friction.total == pytest.approx(500.5)

    def test_confirmed_with_hold_delay(self):
        """Confirmed operation delayed by HOLD.

        EVP Gross = 1000 × 1.0 = 1000
        EVP Net   = 950 − 0 = 950
        EVP Ratio = 950 / 1000 = 0.95
        Friction  = 120 × 0.001 (hold delay) + 0 + 0 + 0 + 0.5
                  = 0.12 + 0.5 = 0.62
        """
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=950.0,
            confidence=1.0,
            hold_duration_seconds=120.0,
        )
        assert report.gross.expected_value == 1000.0
        assert report.net.net_value == 950.0
        assert report.evp_ratio == pytest.approx(0.95)
        assert report.friction.hold_delay_cost == pytest.approx(0.12)
        assert report.friction.total == pytest.approx(0.62)

    def test_confirmed_with_escalation_and_reconciliation(self):
        """Confirmed operation that was escalated and reconciled.

        EVP Gross = 5000 × 0.95 = 4750
        EVP Net   = 4800 − 100 = 4700
        EVP Ratio = 4700 / 4750 = 0.989...
        Friction  = 0 + 300 × 0.01 (escalation) + 2 × 5.0 (reconciliation)
                   + 0 + 0.5
                  = 3.0 + 10.0 + 0.5 = 13.5
        """
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=5000.0,
            realized_value=4800.0,
            confidence=0.95,
            escalated_duration_seconds=300.0,
            reconciliation_count=2,
            execution_loss=100.0,
        )
        assert report.gross.expected_value == 4750.0
        assert report.net.net_value == 4700.0
        assert report.evp_ratio == pytest.approx(4700.0 / 4750.0)
        assert report.friction.escalation_overhead == pytest.approx(3.0)
        assert report.friction.reconciliation_effort == pytest.approx(10.0)
        assert report.friction.total == pytest.approx(13.5)

    def test_friction_does_not_affect_evp_ratio(self):
        """P1-3: Friction is reported separately, NOT subtracted from EVP Net.

        Two operations with identical gross/net but different friction
        must have the same EVP ratio.
        """
        assessor = EVPAssessor()
        low_friction = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=900.0,
            hold_duration_seconds=0.0,
        )
        high_friction = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=900.0,
            hold_duration_seconds=500.0,
            escalated_duration_seconds=200.0,
            reconciliation_count=5,
        )
        # Same EVP ratio despite different friction
        assert low_friction.evp_ratio == high_friction.evp_ratio
        # But different friction costs
        assert low_friction.friction.total < high_friction.friction.total


class TestEVPSeparationFromAdmissibility:
    """Test the critical doctrine constraint: EVP is NOT an input to admissibility."""

    def test_evp_report_always_marks_is_input_to_admissibility_false(self):
        assessor = EVPAssessor()
        for state in ["confirmed", "failed"]:
            report = assessor.assess(
                operation_state=state,
                intended_value=1000.0,
            )
            assert report.is_input_to_admissibility is False

    def test_high_evp_does_not_justify_inadmissible(self):
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1_000_000.0,
            realized_value=1_000_000.0,
            confidence=1.0,
        )
        assert report.evp_ratio == 1.0
        assert report.is_input_to_admissibility is False

    def test_evp_and_tau_inversion_dont_imply_each_other(self):
        """A transition can have good EVP but be irreversible, and vice versa."""
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
        )
        assert report.evp_ratio == 1.0
        # The EVP report says nothing about reversibility — that's τ⁻¹'s job.