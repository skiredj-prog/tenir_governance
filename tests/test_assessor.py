"""Comprehensive tests for the PostExecutionAssessor.

Per doctrine: the assessor ties τ⁻¹ and EVP together. It runs AFTER
the dispatch state machine reaches a terminal state (CONFIRMED or FAILED).
It does NOT feed back into the admissibility function.

P1 fixes applied:
- P1-1: Failed operations without explicit no_external_effect=True now
  return INDETERMINATE, not PRE_COMMIT.
- P1-2: Empty Q returns INDETERMINATE.
- P1-3: EVP friction is reported separately, not subtracted from EVP Net.
"""
import pytest
from tenir_governance.assessment.assessor import (
    PostExecutionAssessor,
    AssessmentReport,
)
from tenir_governance.assessment.tau_inversion import InversionClassification
from tenir_governance.assessment.evp import GovernanceDebt


class TestPostExecutionAssessor:
    """Test the combined τ⁻¹ + EVP assessor."""

    def test_assess_confirmed_operation(self):
        assessor = PostExecutionAssessor()

        def return_transition(metadata):
            return (0.0, 5.0, "Full refund available")

        report = assessor.assess(
            idempotency_key="op-123",
            passage_id="pass-456",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
            intended_value=1000.0,
            realized_value=950.0,
        )
        assert report.idempotency_key == "op-123"
        assert report.passage_id == "pass-456"
        assert report.operation_state == "confirmed"
        assert report.tau_inversion.classification == InversionClassification.EXACT_INVERSE
        assert report.evp.evp_ratio == pytest.approx(0.95)
        assert report.is_input_to_admissibility is False

    def test_assess_failed_operation(self):
        """P1-1: Failed operation without explicit no_external_effect → INDETERMINATE."""
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-456",
            passage_id="pass-789",
            operation_state="failed",
            retained_properties=["balance"],
            intended_value=1000.0,
            denied_opportunity_value=1000.0,
            # No no_external_effect metadata → INDETERMINATE
        )
        assert report.operation_state == "failed"
        assert report.tau_inversion.classification == InversionClassification.INDETERMINATE
        assert report.evp.evp_ratio == 0.0
        assert report.is_input_to_admissibility is False

    def test_assess_failed_with_explicit_no_effect_is_pre_commit(self):
        """P1-1: Failed operation WITH explicit no_external_effect=True → PRE_COMMIT."""
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-pre",
            passage_id="pass-pre",
            operation_state="failed",
            retained_properties=["balance"],
            intended_value=1000.0,
            denied_opportunity_value=1000.0,
            metadata={"no_external_effect": True},
        )
        assert report.tau_inversion.classification == InversionClassification.PRE_COMMIT

    def test_assess_irreversible_operation(self):
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-789",
            passage_id="pass-abc",
            operation_state="confirmed",
            retained_properties=["message_read_status"],
            return_transition=None,
            intended_value=500.0,
            realized_value=500.0,
        )
        assert report.tau_inversion.classification == InversionClassification.IRREVERSIBLE
        assert report.evp.evp_ratio == 1.0  # Good EVP despite irreversibility

    def test_assess_compensation_operation(self):
        assessor = PostExecutionAssessor()

        def return_transition(metadata):
            return (0.3, 2.0, "Partial refund with FX loss")

        report = assessor.assess(
            idempotency_key="op-comp",
            passage_id="pass-comp",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
            intended_value=1000.0,
            realized_value=900.0,
        )
        assert report.tau_inversion.classification == InversionClassification.COMPENSATION
        assert report.evp.evp_ratio == pytest.approx(0.9)

    def test_assess_empty_q_is_indeterminate(self):
        """P1-2: Empty Q → INDETERMINATE for τ⁻¹."""
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-empty-q",
            passage_id="pass-eq",
            operation_state="confirmed",
            retained_properties=[],  # Empty Q
            return_transition=lambda m: (0.0, 0.0, ""),
            intended_value=1000.0,
            realized_value=1000.0,
        )
        assert report.tau_inversion.classification == InversionClassification.INDETERMINATE

    def test_is_input_to_admissibility_always_false(self):
        assessor = PostExecutionAssessor()
        for state in ["confirmed", "failed"]:
            report = assessor.assess(
                idempotency_key=f"op-{state}",
                passage_id="pass",
                operation_state=state,
                retained_properties=["balance"],
                intended_value=1000.0,
            )
            assert report.is_input_to_admissibility is False

    def test_report_has_timestamp(self):
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-ts",
            passage_id="pass",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
            intended_value=100.0,
        )
        assert report.assessment_timestamp != ""
        assert "T" in report.assessment_timestamp

    def test_report_to_dict(self):
        assessor = PostExecutionAssessor()

        def return_transition(metadata):
            return (0.0, 1.0, "Reversal")

        report = assessor.assess(
            idempotency_key="op-dict",
            passage_id="pass-dict",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=return_transition,
            intended_value=100.0,
            realized_value=100.0,
        )
        d = report.to_dict()
        assert d["idempotency_key"] == "op-dict"
        assert d["operation_state"] == "confirmed"
        assert d["is_input_to_admissibility"] is False
        assert d["tau_inversion"]["classification"] == "exact_inverse"
        assert "evp" in d
        assert "rationale" in d

    def test_combined_rationale_mentions_both_assessments(self):
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-rat",
            passage_id="pass",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
            intended_value=1000.0,
            realized_value=900.0,
        )
        assert "τ⁻¹" in report.rationale or "tau" in report.rationale.lower()
        assert "EVP" in report.rationale or "evp" in report.rationale.lower()
        assert "admissibility" in report.rationale.lower()


class TestAssessorReportStorage:
    """Test report storage and retrieval."""

    def test_report_count(self):
        assessor = PostExecutionAssessor()
        for i in range(5):
            assessor.assess(
                idempotency_key=f"op-{i}",
                passage_id="pass",
                operation_state="confirmed",
                retained_properties=["balance"],
                return_transition=lambda m: (0.0, 1.0, ""),
                intended_value=100.0,
            )
        assert assessor.report_count == 5

    def test_get_report_by_key(self):
        assessor = PostExecutionAssessor()
        assessor.assess(
            idempotency_key="op-a",
            passage_id="pass",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
            intended_value=100.0,
        )
        assessor.assess(
            idempotency_key="op-b",
            passage_id="pass",
            operation_state="failed",
            retained_properties=["balance"],
            intended_value=200.0,
        )
        report = assessor.get_report("op-a")
        assert report is not None
        assert report.idempotency_key == "op-a"
        assert report.operation_state == "confirmed"

    def test_get_report_not_found(self):
        assessor = PostExecutionAssessor()
        assert assessor.get_report("nonexistent") is None

    def test_get_all_reports_for_key(self):
        assessor = PostExecutionAssessor()
        for i in range(3):
            assessor.assess(
                idempotency_key="op-multi",
                passage_id="pass",
                operation_state="confirmed",
                retained_properties=["balance"],
                return_transition=lambda m: (0.0, 1.0, ""),
                intended_value=100.0 * i,
            )
        reports = assessor.get_all_reports("op-multi")
        assert len(reports) == 3

    def test_clear(self):
        assessor = PostExecutionAssessor()
        assessor.assess(
            idempotency_key="op-1",
            passage_id="pass",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
            intended_value=100.0,
        )
        assert assessor.report_count == 1
        assessor.clear()
        assert assessor.report_count == 0


class TestAssessorAggregateStats:
    """Test aggregate statistics across multiple reports."""

    def test_empty_stats(self):
        assessor = PostExecutionAssessor()
        stats = assessor.aggregate_stats()
        assert stats["total_reports"] == 0

    def test_confirmed_failed_counts(self):
        assessor = PostExecutionAssessor()
        assessor.assess(
            idempotency_key="op-1", passage_id="p",
            operation_state="confirmed", intended_value=100.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )
        assessor.assess(
            idempotency_key="op-2", passage_id="p",
            operation_state="confirmed", intended_value=200.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )
        assessor.assess(
            idempotency_key="op-3", passage_id="p",
            operation_state="failed", intended_value=300.0,
            retained_properties=["balance"],
        )
        stats = assessor.aggregate_stats()
        assert stats["confirmed_count"] == 2
        assert stats["failed_count"] == 1
        assert stats["total_reports"] == 3

    def test_tau_distribution(self):
        """P1-1: Failed without explicit no_external_effect → INDETERMINATE, not PRE_COMMIT."""
        assessor = PostExecutionAssessor()

        # EXACT_INVERSE
        assessor.assess(
            idempotency_key="op-1", passage_id="p",
            operation_state="confirmed", intended_value=100.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )
        # IRREVERSIBLE
        assessor.assess(
            idempotency_key="op-2", passage_id="p",
            operation_state="confirmed", intended_value=100.0,
            retained_properties=["balance"],
            return_transition=None,
        )
        # INDETERMINATE (failed without explicit no_external_effect)
        assessor.assess(
            idempotency_key="op-3", passage_id="p",
            operation_state="failed", intended_value=100.0,
            retained_properties=["balance"],
        )
        # PRE_COMMIT (failed WITH explicit no_external_effect=True)
        assessor.assess(
            idempotency_key="op-4", passage_id="p",
            operation_state="failed", intended_value=100.0,
            retained_properties=["balance"],
            metadata={"no_external_effect": True},
        )

        stats = assessor.aggregate_stats()
        assert stats["tau_distribution"]["exact_inverse"] == 1
        assert stats["tau_distribution"]["irreversible"] == 1
        assert stats["tau_distribution"]["indeterminate"] == 1
        assert stats["tau_distribution"]["pre_commit"] == 1

    def test_avg_evp_ratio(self):
        assessor = PostExecutionAssessor()
        assessor.assess(
            idempotency_key="op-1", passage_id="p",
            operation_state="confirmed", intended_value=1000.0,
            realized_value=1000.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )  # ratio = 1.0
        assessor.assess(
            idempotency_key="op-2", passage_id="p",
            operation_state="confirmed", intended_value=1000.0,
            realized_value=500.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )  # ratio = 0.5
        stats = assessor.aggregate_stats()
        assert stats["avg_evp_ratio"] == pytest.approx(0.75)

    def test_total_friction_cost(self):
        assessor = PostExecutionAssessor()
        assessor.assess(
            idempotency_key="op-1", passage_id="p",
            operation_state="confirmed", intended_value=1000.0,
            realized_value=1000.0,
            hold_duration_seconds=100.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )
        stats = assessor.aggregate_stats()
        assert stats["total_friction_cost"] > 0.0


class TestAssessorNonInterference:
    """Test that the assessor does NOT interfere with the state machine."""

    def test_assess_does_not_modify_operation_state(self):
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-test",
            passage_id="pass-test",
            operation_state="confirmed",
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
            intended_value=100.0,
        )
        assert report.operation_state == "confirmed"
        assert not hasattr(assessor, 'registry') or assessor.__dict__.get('registry') is None

    def test_assess_is_pure_measurement(self):
        assessor = PostExecutionAssessor()
        r1 = assessor.assess(
            idempotency_key="op-1", passage_id="p",
            operation_state="confirmed", intended_value=100.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )
        r2 = assessor.assess(
            idempotency_key="op-2", passage_id="p",
            operation_state="confirmed", intended_value=200.0,
            retained_properties=["balance"],
            return_transition=lambda m: (0.0, 1.0, ""),
        )
        assert r1.evp.gross.intended_value == 100.0
        assert r2.evp.gross.intended_value == 200.0
        assert assessor.get_report("op-1").evp.gross.intended_value == 100.0