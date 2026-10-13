"""Post-execution assessor — ties τ⁻¹ and EVP together.

Per doctrine: both τ⁻¹ (partial tau inversion) and EVP (intended value
preservation) are EX-POST assessments. They run AFTER the dispatch state
machine reaches a terminal state (CONFIRMED or FAILED), never before.

Critical doctrine constraints enforced by this module:
    1. The assessor runs ONLY after CONFIRMED or FAILED. It does NOT
       run during adjudication, HOLD, DISPATCHING, or UNKNOWN states.
    2. The assessment report does NOT feed back into the admissibility
       function D. It is a pure measurement, not a gate.
    3. τ⁻¹ and EVP do NOT imply each other. A transition can have τ⁻¹
       but poor EVP, or vice versa.

The assessor is OPTIONAL. The governor and SDK client can operate
without it. When configured, it produces an AssessmentReport after
each terminal operation.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
It is NOT canonical, NOT doctrinal. It carries implementation-level
governance enforcement authority only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .tau_inversion import (
    TauInversionClassifier,
    TauInversionResult,
    InversionClassification,
)
from .evp import (
    EVPAssessor,
    EVPReport,
    EVPGross,
    EVPNet,
    FrictionCost,
    GovernanceDebt,
)


@dataclass
class AssessmentReport:
    """Complete post-execution assessment for a single operation.

    Contains both the τ⁻¹ classification and the EVP measurement, plus
    metadata about the operation. This report is produced AFTER the
    operation reaches a terminal state.

    CRITICAL: This report does NOT feed back into the admissibility
    function. The is_input_to_admissibility field is always False.

    Attributes:
        idempotency_key: The operation's idempotency key.
        passage_id: The passage that initiated the operation.
        operation_state: Terminal state ("confirmed" or "failed").
        tau_inversion: τ⁻¹ classification result.
        evp: EVP measurement report.
        assessment_timestamp: ISO timestamp when the assessment was produced.
        is_input_to_admissibility: Always False — doctrine constraint.
        rationale: Human-readable summary combining both assessments.
    """
    idempotency_key: str = ""
    passage_id: str = ""
    operation_state: str = ""
    tau_inversion: TauInversionResult | None = None
    evp: EVPReport | None = None
    assessment_timestamp: str = ""
    is_input_to_admissibility: bool = False  # ALWAYS False — doctrine constraint
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "passage_id": self.passage_id,
            "operation_state": self.operation_state,
            "tau_inversion": self.tau_inversion.to_dict() if self.tau_inversion else None,
            "evp": self.evp.to_dict() if self.evp else None,
            "assessment_timestamp": self.assessment_timestamp,
            "is_input_to_admissibility": self.is_input_to_admissibility,
            "rationale": self.rationale,
        }


class PostExecutionAssessor:
    """Runs post-execution assessment (τ⁻¹ + EVP) after terminal states.

    This assessor is designed to be called AFTER the dispatch state machine
    reaches a terminal state (CONFIRMED or FAILED). It produces an
    AssessmentReport containing both the τ⁻¹ classification and the EVP
    measurement.

    The assessor is OPTIONAL. The governor operates correctly without it.
    When configured, it enriches the post-execution audit trail without
    interfering with the state machine.

    Usage:
        assessor = PostExecutionAssessor()
        report = assessor.assess(
            idempotency_key="op-123",
            passage_id="pass-456",
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=950.0,
            retained_properties=["balance", "audit_trail"],
            return_transition=my_return_fn,
        )
        # report.tau_inversion.classification -> EXACT_INVERSE
        # report.evp.evp_ratio -> 0.948

    Integration with governor:
        governor = DualKernelGovernor(...)
        governor.assessor = PostExecutionAssessor()
        # After dispatch_and_execute():
        result = governor.dispatch_and_execute("op-123")
        if result["operation_state"] in ("confirmed", "failed"):
            report = governor.assess_post_execution("op-123", ...)
    """

    def __init__(
        self,
        tau_classifier: TauInversionClassifier | None = None,
        evp_assessor: EVPAssessor | None = None,
    ):
        """
        Args:
            tau_classifier: τ⁻¹ classifier. If None, uses default.
            evp_assessor: EVP assessor. If None, uses default.
        """
        self.tau_classifier = tau_classifier or TauInversionClassifier()
        self.evp_assessor = evp_assessor or EVPAssessor()
        self._reports: list[AssessmentReport] = []

    def assess(
        self,
        idempotency_key: str,
        passage_id: str,
        operation_state: str,
        # τ⁻¹ parameters
        retained_properties: list[str] | None = None,
        return_transition: Callable[..., tuple[float, float, str]] | None = None,
        non_deterministic_results: list[dict[str, Any]] | None = None,
        # EVP parameters
        intended_value: float = 0.0,
        realized_value: float | None = None,
        confidence: float = 1.0,
        hold_duration_seconds: float = 0.0,
        escalated_duration_seconds: float = 0.0,
        reconciliation_count: int = 0,
        execution_loss: float = 0.0,
        denied_opportunity_value: float = 0.0,
        friction_config: dict[str, float] | None = None,
        governance_debt: GovernanceDebt | None = None,
        # Shared
        metadata: dict[str, Any] | None = None,
    ) -> AssessmentReport:
        """Run both τ⁻¹ and EVP assessments for a completed operation.

        This method runs AFTER the operation reaches a terminal state.
        It does NOT modify the operation's state or feed back into
        admissibility.

        Args:
            idempotency_key: The operation's idempotency key.
            passage_id: The passage that initiated the operation.
            operation_state: Terminal state ("confirmed" or "failed").
            retained_properties: Properties Q for τ⁻¹ evaluation.
            return_transition: Return transition function for τ⁻¹.
            non_deterministic_results: For non-deterministic transitions.
            intended_value: Value the operation was designed to produce.
            realized_value: Actual value produced.
            confidence: Prior confidence of success (0-1).
            hold_duration_seconds: Time in HOLD.
            escalated_duration_seconds: Time in ESCALATED.
            reconciliation_count: Number of reconciliations.
            execution_loss: Value lost to execution failures.
            denied_opportunity_value: Value of denied opportunities.
            friction_config: Override friction rates.
            governance_debt: Current governance debt snapshot.
            metadata: Additional context for both assessments.

        Returns:
            AssessmentReport with both τ⁻¹ and EVP results.
        """
        from datetime import datetime, timezone

        metadata = metadata or {}

        # ── τ⁻¹ assessment ─────────────────────────────────────────────
        tau_result = self.tau_classifier.classify(
            operation_state=operation_state,
            action_type=metadata.get("action_type", ""),
            retained_properties=retained_properties,
            return_transition=return_transition,
            non_deterministic_results=non_deterministic_results,
            metadata=metadata,
        )

        # ── EVP assessment ──────────────────────────────────────────────
        evp_report = self.evp_assessor.assess(
            operation_state=operation_state,
            intended_value=intended_value,
            realized_value=realized_value,
            confidence=confidence,
            hold_duration_seconds=hold_duration_seconds,
            escalated_duration_seconds=escalated_duration_seconds,
            reconciliation_count=reconciliation_count,
            execution_loss=execution_loss,
            denied_opportunity_value=denied_opportunity_value,
            friction_config=friction_config,
            governance_debt=governance_debt,
            description=metadata.get("intended_value_description", ""),
        )

        # ── Combined rationale ──────────────────────────────────────────
        tau_desc = tau_result.classification.value
        evp_ratio = evp_report.evp_ratio
        loss_str = f"{tau_result.loss:.4f}" if tau_result.loss is not None else "N/A (irreversible)"
        rationale = (
            f"Post-execution assessment for operation '{idempotency_key}' "
            f"(state: {operation_state}). "
            f"τ⁻¹ classification: {tau_desc} "
            f"(reversible: {tau_result.is_reversible}, "
            f"loss: {loss_str}). "
            f"EVP ratio: {evp_ratio:.3f} "
            f"(gross: {evp_report.gross.expected_value:.2f}, "
            f"net: {evp_report.net.net_value:.2f}, "
            f"friction: {evp_report.friction.total:.2f}). "
            f"Neither assessment feeds back into admissibility."
        )

        report = AssessmentReport(
            idempotency_key=idempotency_key,
            passage_id=passage_id,
            operation_state=operation_state,
            tau_inversion=tau_result,
            evp=evp_report,
            assessment_timestamp=datetime.now(timezone.utc).isoformat(),
            is_input_to_admissibility=False,  # ALWAYS False — doctrine constraint
            rationale=rationale,
        )

        self._reports.append(report)
        return report

    @property
    def report_count(self) -> int:
        """Number of assessment reports produced."""
        return len(self._reports)

    @property
    def reports(self) -> list[AssessmentReport]:
        """All assessment reports produced so far (read-only view)."""
        return list(self._reports)

    def get_report(self, idempotency_key: str) -> AssessmentReport | None:
        """Get the most recent assessment report for an operation."""
        for report in reversed(self._reports):
            if report.idempotency_key == idempotency_key:
                return report
        return None

    def get_all_reports(self, idempotency_key: str) -> list[AssessmentReport]:
        """Get all assessment reports for an operation (in chronological order)."""
        return [r for r in self._reports if r.idempotency_key == idempotency_key]

    def clear(self):
        """Clear all stored reports. Useful for testing."""
        self._reports.clear()

    def aggregate_stats(self) -> dict[str, Any]:
        """Compute aggregate statistics across all reports.

        Returns:
            Dict with:
            - total_reports: Total number of reports.
            - confirmed_count: Reports for confirmed operations.
            - failed_count: Reports for failed operations.
            - tau_distribution: Count of each τ⁻¹ classification.
            - avg_evp_ratio: Average EVP ratio across all reports.
            - avg_tau_loss: Average τ⁻¹ loss (for reversible operations).
            - total_friction_cost: Sum of all friction costs.
            - total_governance_debt: Latest governance debt total.
        """
        if not self._reports:
            return {
                "total_reports": 0,
                "confirmed_count": 0,
                "failed_count": 0,
                "tau_distribution": {},
                "avg_evp_ratio": 0.0,
                "avg_tau_loss": 0.0,
                "total_friction_cost": 0.0,
                "total_governance_debt": 0.0,
            }

        confirmed = sum(1 for r in self._reports if r.operation_state == "confirmed")
        failed = sum(1 for r in self._reports if r.operation_state == "failed")

        tau_dist: dict[str, int] = {}
        for r in self._reports:
            if r.tau_inversion:
                cls = r.tau_inversion.classification.value
                tau_dist[cls] = tau_dist.get(cls, 0) + 1

        evp_ratios = [r.evp.evp_ratio for r in self._reports if r.evp]
        avg_evp = sum(evp_ratios) / len(evp_ratios) if evp_ratios else 0.0

        tau_losses = [
            r.tau_inversion.loss for r in self._reports
            if r.tau_inversion and r.tau_inversion.loss is not None
        ]
        avg_tau_loss = sum(tau_losses) / len(tau_losses) if tau_losses else 0.0

        total_friction = sum(
            r.evp.friction.total for r in self._reports if r.evp
        )

        latest_debt = 0.0
        for r in reversed(self._reports):
            if r.evp and r.evp.governance_debt:
                latest_debt = r.evp.governance_debt.total_debt
                break

        return {
            "total_reports": len(self._reports),
            "confirmed_count": confirmed,
            "failed_count": failed,
            "tau_distribution": tau_dist,
            "avg_evp_ratio": round(avg_evp, 4),
            "avg_tau_loss": round(avg_tau_loss, 4),
            "total_friction_cost": round(total_friction, 4),
            "total_governance_debt": round(latest_debt, 4),
        }