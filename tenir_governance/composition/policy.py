"""Governance policy — explicit, versioned, configurable.

Per review 2 §3: the 85% budget escalation threshold is a configurable, versioned
policy parameter, not a universal safety boundary. It must be explicit and
testable at boundary conditions.
"""
# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
#
# KB3 posture mapping (implementation → canonical):
#   PASS → NORMAL
#   FLAG → CAUTION
#   HOLD → HOLDING-FIRST
#   HARD_VETO → HOLDING-FIRST (block at execution layer)
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompositionPolicy:
    """Versioned policy contract for the composition layer.

    Attributes:
        version: Policy version string for ledger binding and audit.
        budget_escalation_threshold: Budget utilization fraction (0.0–1.0) above
            which a FLAG from either kernel escalates to HOLD. Default 0.85.
        fail_closed_on_error: If True (default), any kernel exception or
            indeterminate result produces HOLD, never PASS.
        retain_all_reasons: If True (default), the composed rationale retains
            both the HARD_VETO reason and the HOLD reason when they coexist.
        revalidate_budget_at_dispatch: If True (default), budget is rechecked
            at the execution boundary, not just at adjudication time.
    """
    version: str = "composition-v1.0.0"
    budget_escalation_threshold: float = 0.85
    fail_closed_on_error: bool = True
    retain_all_reasons: bool = True
    revalidate_budget_at_dispatch: bool = True

    def validate(self) -> None:
        assert 0.0 <= self.budget_escalation_threshold <= 1.0, (
            "budget_escalation_threshold must be in [0.0, 1.0]"
        )