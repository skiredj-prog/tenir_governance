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
# Execution verdict and posture are distinct fields. They are computed
# and returned separately; this module does not map one onto the other.
#   Verdict (what is): PASS / FLAG / HOLD / HARD_VETO
#   Posture (what we do): NORMAL / CAUTION / HOLDING-FIRST
# schema_adapter.py returns posture in its own field.
from __future__ import annotations

import math
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
        hold_tau_k_seconds: Fail-safe bound, in seconds, on how long a HOLD
            created by this governor may wait for its human resolution (τ_K).
            None (default) declares no bound: the governor then invents none,
            and behaviour is unchanged from 5.7.0. When set, its exhaustion
            refuses alone wherever the held operation could advance
            (re-adjudication, authorized exit, dispatch): the operation fails
            closed and its reservation is released. No timer is required.
        hold_tau_k_source: Where the organization derived τ_K from (SLA,
            regulation, ...). Recorded on each HOLD. Required with
            hold_tau_k_seconds, so that a bound is never anonymous.
        flag_conditions: The explicit conditions or supervision under which a
            FLAG authorizes (RFC-0 §4), declared by the deployment. Copied onto
            every FLAG result (``flag_conditions``) and into its ledgered
            rationale. Empty (default): the runtime invents none, and a FLAG
            then states no condition, which REG records as an implementation
            gap. The runtime records these conditions; it does not enforce them.
    """
    version: str = "composition-v1.0.0"
    budget_escalation_threshold: float = 0.85
    fail_closed_on_error: bool = True
    retain_all_reasons: bool = True
    revalidate_budget_at_dispatch: bool = True
    hold_tau_k_seconds: float | None = None
    hold_tau_k_source: str = ""
    flag_conditions: tuple[str, ...] = ()

    def validate(self) -> None:
        assert 0.0 <= self.budget_escalation_threshold <= 1.0, (
            "budget_escalation_threshold must be in [0.0, 1.0]"
        )
        if self.hold_tau_k_seconds is not None:
            tau = self.hold_tau_k_seconds
            if isinstance(tau, bool) or not isinstance(tau, (int, float)):
                raise ValueError("hold_tau_k_seconds must be a number of seconds or None")
            if not math.isfinite(tau) or tau <= 0:
                raise ValueError("hold_tau_k_seconds must be finite and > 0, or None")
            if not str(self.hold_tau_k_source).strip():
                raise ValueError("hold_tau_k_source is required when hold_tau_k_seconds is set")
        if not isinstance(self.flag_conditions, tuple) or not all(
            isinstance(c, str) and c.strip() for c in self.flag_conditions
        ):
            raise ValueError("flag_conditions must be a tuple of non-empty strings")