"""Comprehensive tests for EVP (Intended Value Preservation).

Per doctrine (TENIR Property-Preserving Transitions Manuscript, Layer 4):
- EVP measures intended value protection as an OUTCOME of governance discipline.
- EVP ∉ Admissibility Function. Admissibility precedes value measurement.
- High expected value never justifies an otherwise inadmissible transition.
- EVP Gross / EVP Net, friction costs, Governance Debt are consequences,
  not inputs to D.
- EVP and τ⁻¹ don't imply each other.
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
    """Test EVP Net — actual value realized after friction."""

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
    """Test friction costs — overhead imposed by governance discipline."""

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
    """Test the complete EVP report."""

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


class TestEVPSeparationFromAdmissibility:
    """Test the critical doctrine constraint: EVP is NOT an input to admissibility.

    Per doctrine: "EVP ∉ Admissibility Function. Admissibility precedes value
    measurement. High expected value never justifies an otherwise inadmissible
    transition."
    """

    def test_evp_report_always_marks_is_input_to_admissibility_false(self):
        """Every EVP report must explicitly state it is not an admissibility input."""
        assessor = EVPAssessor()
        for state in ["confirmed", "failed"]:
            report = assessor.assess(
                operation_state=state,
                intended_value=1000.0,
            )
            assert report.is_input_to_admissibility is False, (
                f"EVP report for {state} must not be input to admissibility"
            )

    def test_high_evp_does_not_justify_inadmissible(self):
        """Even with very high EVP, the report does not become an admissibility input.

        This is a doctrinal assertion: the EVP measurement, no matter how
        favorable, cannot be used to justify bypassing the admissibility
        function. The is_input_to_admissibility field enforces this.
        """
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1_000_000.0,
            realized_value=1_000_000.0,
            confidence=1.0,
        )
        assert report.evp_ratio == 1.0  # Perfect EVP
        assert report.is_input_to_admissibility is False  # Still not an input

    def test_evp_and_tau_inversion_dont_imply_each_other(self):
        """A transition can have good EVP but be irreversible, and vice versa.

        This test verifies that the EVP module does not make assumptions
        about τ⁻¹. They are independent measurements.
        """
        assessor = EVPAssessor()
        # Good EVP but the operation is irreversible (would be determined
        # by τ⁻¹ separately)
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=1000.0,
        )
        assert report.evp_ratio == 1.0
        # The EVP report says nothing about reversibility — that's τ⁻¹'s job.
        # There is no reversibility field in EVPReport.