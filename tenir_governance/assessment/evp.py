"""EVP — Intended Value Preservation (post-execution assessment).

Per doctrine (TENIR Property-Preserving Transitions Manuscript, Layer 4):

    "EVP measures intended value protection as an outcome of governance
     discipline. Critical separation: EVP ∉ Admissibility Function.
     Admissibility precedes value measurement. High expected value never
     justifies an otherwise inadmissible transition. EVP Gross / EVP Net,
     friction costs, and Governance Debt are reported as consequences,
     not as inputs to D."

    "EVP: Outcome → Value Protection (consequence; never an input to
     admissibility)."

Per v7.1: "EVP. Downstream value measurement is not treated." — EVP is
explicitly deferred in the v7.1 manuscript. This implementation provides
the measurement infrastructure; it does NOT feed back into the admissibility
function.

Key concepts:
    - EVP Gross: The total intended value of the operation if it had
      executed perfectly without governance friction.
    - EVP Net: The actual value realized after governance friction costs
      and any losses from delayed/denied/held operations.
    - Friction Costs: The overhead imposed by governance discipline —
      HOLD durations, escalation overhead, reconciliation effort,
      denied-opportunity cost.
    - Governance Debt: The accumulated cost of unresolved governance
      actions — operations in HOLD, ESCALATED, or UNKNOWN that are
      consuming resources without producing value.

CRITICAL: EVP is an EX-POST (post-execution) measurement. It runs AFTER
the dispatch state machine reaches a terminal state. It is NOT an input
to the admissibility function D. It does NOT affect PASS/FLAG/HOLD/HARD_VETO.
It does NOT imply τ⁻¹, and τ⁻¹ does not imply it.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
It is NOT canonical, NOT doctrinal. It carries implementation-level
governance enforcement authority only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class EVPGross:
    """EVP Gross — the total intended value of the operation.

    This is the value that would have been realized if the operation
    executed perfectly, with no governance friction, no delays, no
    holds, and no failures.

    Attributes:
        intended_value: The value the operation was designed to produce.
        confidence: The prior confidence that the operation would succeed
            (0.0 to 1.0). Used to compute expected gross value.
        description: Human-readable description of the intended value.
    """
    intended_value: float = 0.0
    confidence: float = 1.0
    description: str = ""

    @property
    def expected_value(self) -> float:
        """Expected gross value = intended_value × confidence."""
        return self.intended_value * self.confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "intended_value": self.intended_value,
            "confidence": self.confidence,
            "expected_value": self.expected_value,
            "description": self.description,
        }


@dataclass
class FrictionCost:
    """Friction costs — overhead imposed by governance discipline.

    Per doctrine: governance discipline is not free. It imposes costs
    in the form of delays, overhead, denied opportunities, and
    reconciliation effort. These costs are reported as consequences,
    not as reasons to bypass governance.

    Attributes:
        hold_delay_cost: Value lost due to HOLD duration (time-value
            of money, opportunity cost during hold).
        escalation_overhead: Cost of escalation (human review time,
            risk officer involvement, ceremony overhead).
        reconciliation_effort: Cost of reconciling UNKNOWN operations
            (provider queries, manual investigation).
        denied_opportunity_cost: Value of opportunities that were
            denied by governance (HARD_VETO, expired HOLD).
        processing_overhead: Computational/operational overhead of
            running both kernels, ledger, registry.
    """
    hold_delay_cost: float = 0.0
    escalation_overhead: float = 0.0
    reconciliation_effort: float = 0.0
    denied_opportunity_cost: float = 0.0
    processing_overhead: float = 0.0

    @property
    def total(self) -> float:
        """Total friction cost."""
        return (
            self.hold_delay_cost
            + self.escalation_overhead
            + self.reconciliation_effort
            + self.denied_opportunity_cost
            + self.processing_overhead
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hold_delay_cost": self.hold_delay_cost,
            "escalation_overhead": self.escalation_overhead,
            "reconciliation_effort": self.reconciliation_effort,
            "denied_opportunity_cost": self.denied_opportunity_cost,
            "processing_overhead": self.processing_overhead,
            "total": self.total,
        }


@dataclass
class EVPNet:
    """EVP Net — the actual value realized after friction.

    EVP Net = EVP Gross expected value − friction costs − execution losses.

    Attributes:
        realized_value: The actual value produced by the operation.
        execution_loss: Value lost due to execution failures (partial
            execution, provider errors, timeout losses).
    """
    realized_value: float = 0.0
    execution_loss: float = 0.0

    @property
    def net_value(self) -> float:
        """Net value = realized_value − execution_loss."""
        return self.realized_value - self.execution_loss

    def to_dict(self) -> dict[str, Any]:
        return {
            "realized_value": self.realized_value,
            "execution_loss": self.execution_loss,
            "net_value": self.net_value,
        }


@dataclass
class GovernanceDebt:
    """Governance Debt — accumulated cost of unresolved governance actions.

    Per doctrine: governance debt accumulates when operations are in
    unresolved states (HOLD, ESCALATED, UNKNOWN). These operations consume
    resources (locked budgets, human attention, system capacity) without
    producing value. The debt is the opportunity cost of these unresolved
    operations.

    This is a running tally, not per-operation. It accumulates across
    the lifecycle of the governance system.

    Attributes:
        unresolved_hold_count: Number of operations currently in HOLD.
        unresolved_escalated_count: Number in ESCALATED.
        unresolved_unknown_count: Number in UNKNOWN.
        locked_budget: Total budget locked by unresolved operations.
        accumulated_delay_cost: Total opportunity cost of delays so far.
    """
    unresolved_hold_count: int = 0
    unresolved_escalated_count: int = 0
    unresolved_unknown_count: int = 0
    locked_budget: float = 0.0
    accumulated_delay_cost: float = 0.0

    @property
    def total_debt(self) -> float:
        """Total governance debt = locked_budget + accumulated_delay_cost."""
        return self.locked_budget + self.accumulated_delay_cost

    @property
    def total_unresolved(self) -> int:
        """Total unresolved operations across all non-terminal states."""
        return (
            self.unresolved_hold_count
            + self.unresolved_escalated_count
            + self.unresolved_unknown_count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unresolved_hold_count": self.unresolved_hold_count,
            "unresolved_escalated_count": self.unresolved_escalated_count,
            "unresolved_unknown_count": self.unresolved_unknown_count,
            "total_unresolved": self.total_unresolved,
            "locked_budget": self.locked_budget,
            "accumulated_delay_cost": self.accumulated_delay_cost,
            "total_debt": self.total_debt,
        }


@dataclass
class EVPReport:
    """Complete EVP assessment for a single operation or period.

    This report is produced AFTER execution. It does NOT feed back into
    the admissibility function.

    Attributes:
        gross: EVP Gross — intended value.
        net: EVP Net — realized value after losses.
        friction: Friction costs imposed by governance.
        governance_debt: Current governance debt snapshot.
        is_input_to_admissibility: Always False. This field exists to
            make the doctrine constraint explicit and machine-checkable.
        rationale: Human-readable summary.
    """
    gross: EVPGross = field(default_factory=EVPGross)
    net: EVPNet = field(default_factory=EVPNet)
    friction: FrictionCost = field(default_factory=FrictionCost)
    governance_debt: GovernanceDebt = field(default_factory=GovernanceDebt)
    is_input_to_admissibility: bool = False  # ALWAYS False — doctrine constraint
    rationale: str = ""

    @property
    def evp_ratio(self) -> float:
        """EVP ratio = net_value / expected_gross_value.

        A ratio of 1.0 means the operation realized its full intended
        value. Below 1.0 means friction or execution losses reduced the
        realized value. Above 1.0 is possible if the governance discipline
        prevented a harmful operation (value preservation exceeds intended).

        Returns 0.0 if expected gross value is 0.
        """
        expected = self.gross.expected_value
        if expected == 0.0:
            return 0.0
        return self.net.net_value / expected

    def to_dict(self) -> dict[str, Any]:
        return {
            "gross": self.gross.to_dict(),
            "net": self.net.to_dict(),
            "friction": self.friction.to_dict(),
            "governance_debt": self.governance_debt.to_dict(),
            "is_input_to_admissibility": self.is_input_to_admissibility,
            "evp_ratio": self.evp_ratio,
            "rationale": self.rationale,
        }


class EVPAssessor:
    """Assesses Intended Value Preservation (EVP) for completed operations.

    EVP measures how well governance discipline preserved the intended
    value of operations. It is an EX-POST measurement — it runs after
    execution, not before. It is NOT an input to the admissibility function.

    Usage:
        assessor = EVPAssessor()
        report = assessor.assess(
            operation_state="confirmed",
            intended_value=1000.0,
            realized_value=950.0,
            hold_duration_seconds=120.0,
            friction_config={
                "hold_delay_rate": 0.001,  # per second
                "processing_overhead": 0.5,
            },
        )
        print(report.evp_ratio)  # e.g., 0.948
    """

    def __init__(self, default_friction_config: dict[str, float] | None = None):
        """
        Args:
            default_friction_config: Default configuration for friction
                cost calculation. Keys:
                - hold_delay_rate: cost per second of HOLD (default 0.001)
                - escalation_rate: cost per second of ESCALATED (default 0.01)
                - reconciliation_cost: fixed cost per reconciliation (default 5.0)
                - processing_overhead: fixed cost per adjudication (default 0.5)
        """
        self.default_friction_config = default_friction_config or {
            "hold_delay_rate": 0.001,      # per second
            "escalation_rate": 0.01,       # per second
            "reconciliation_cost": 5.0,   # per reconciliation
            "processing_overhead": 0.5,   # per adjudication
        }

    def assess(
        self,
        operation_state: str,
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
        description: str = "",
    ) -> EVPReport:
        """Assess the EVP of a completed operation.

        Args:
            operation_state: Terminal state ("confirmed" or "failed").
            intended_value: The value the operation was designed to produce.
            realized_value: The actual value produced. If None, defaults to
                intended_value for "confirmed", 0.0 for "failed".
            confidence: Prior confidence the operation would succeed (0-1).
            hold_duration_seconds: Time spent in HOLD state.
            escalated_duration_seconds: Time spent in ESCALATED state.
            reconciliation_count: Number of reconciliation attempts.
            execution_loss: Value lost due to execution failures.
            denied_opportunity_value: Value of opportunities denied by
                governance (HARD_VETO, expired HOLD).
            friction_config: Override default friction rates.
            governance_debt: Current governance debt snapshot. If None,
                a fresh snapshot is created.
            description: Human-readable description of the intended value.

        Returns:
            EVPReport with gross, net, friction, and governance debt.

        EVP calculation:
            EVP Gross = intended_value × confidence
            Friction = hold_delay + escalation + reconciliation + denied + processing
            EVP Net = realized_value − execution_loss
            EVP Ratio = EVP Net / EVP Gross
        """
        config = {**self.default_friction_config, **(friction_config or {})}

        # ── EVP Gross ───────────────────────────────────────────────────
        gross = EVPGross(
            intended_value=intended_value,
            confidence=confidence,
            description=description,
        )

        # ── Realized value ──────────────────────────────────────────────
        if realized_value is None:
            realized_value = intended_value if operation_state == "confirmed" else 0.0

        # ── EVP Net ─────────────────────────────────────────────────────
        net = EVPNet(
            realized_value=realized_value,
            execution_loss=execution_loss,
        )

        # ── Friction Costs ──────────────────────────────────────────────
        hold_delay_cost = hold_duration_seconds * config["hold_delay_rate"]
        escalation_overhead = escalated_duration_seconds * config["escalation_rate"]
        reconciliation_effort = reconciliation_count * config["reconciliation_cost"]
        processing_overhead = config["processing_overhead"]

        friction = FrictionCost(
            hold_delay_cost=hold_delay_cost,
            escalation_overhead=escalation_overhead,
            reconciliation_effort=reconciliation_effort,
            denied_opportunity_cost=denied_opportunity_value,
            processing_overhead=processing_overhead,
        )

        # ── Governance Debt ────────────────────────────────────────────
        if governance_debt is None:
            governance_debt = GovernanceDebt()

        # ── Rationale ───────────────────────────────────────────────────
        ratio = net.net_value / gross.expected_value if gross.expected_value != 0 else 0.0
        if operation_state == "confirmed":
            rationale = (
                f"Operation confirmed. EVP ratio: {ratio:.3f}. "
                f"Intended value {intended_value:.2f}, realized {realized_value:.2f}, "
                f"friction cost {friction.total:.2f}, execution loss {execution_loss:.2f}."
            )
        else:
            rationale = (
                f"Operation failed. EVP ratio: {ratio:.3f}. "
                f"Intended value {intended_value:.2f}, realized 0.00, "
                f"denied opportunity {denied_opportunity_value:.2f}, "
                f"friction cost {friction.total:.2f}."
            )

        return EVPReport(
            gross=gross,
            net=net,
            friction=friction,
            governance_debt=governance_debt,
            is_input_to_admissibility=False,  # ALWAYS False — doctrine constraint
            rationale=rationale,
        )

    def assess_governance_debt(
        self,
        hold_count: int = 0,
        escalated_count: int = 0,
        unknown_count: int = 0,
        locked_budget: float = 0.0,
        accumulated_delay_cost: float = 0.0,
    ) -> GovernanceDebt:
        """Compute a governance debt snapshot.

        This is a system-level measurement, not per-operation. It captures
        the current state of unresolved governance actions.

        Args:
            hold_count: Operations currently in HOLD.
            escalated_count: Operations currently in ESCALATED.
            unknown_count: Operations currently in UNKNOWN.
            locked_budget: Total budget locked by unresolved operations.
            accumulated_delay_cost: Total opportunity cost of delays so far.

        Returns:
            GovernanceDebt snapshot.
        """
        return GovernanceDebt(
            unresolved_hold_count=hold_count,
            unresolved_escalated_count=escalated_count,
            unresolved_unknown_count=unknown_count,
            locked_budget=locked_budget,
            accumulated_delay_cost=accumulated_delay_cost,
        )