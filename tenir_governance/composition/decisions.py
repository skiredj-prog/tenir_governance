"""Decision semantics and execution status — precisely defined.

Per the review: the four decision outcomes must have precise operational meanings,
and policy decisions must be separated from execution status.
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

from enum import Enum


class Decision(str, Enum):
    """Policy decision — whether an action is authorized to execute.

    Operational meanings (per review §3):
        PASS      — All applicable required checks are satisfied; execution may proceed.
        FLAG      — A non-blocking risk or warning exists; proceed only if the
                    applicable policy permits it.
        HOLD      — Do not execute yet. Required evidence, state reconciliation,
                    or human approval is pending. The action is NOT authorized
                    to execute now, but may become eligible after new evidence.
        HARD_VETO  — A mandatory rule is violated, or a prohibited operation is
                    conclusively identified.

    Key distinction: HOLD is not merely a stronger FLAG. It represents a different
    operational state — the action is pending, not denied.
    """
    PASS = "PASS"
    FLAG = "FLAG"
    HOLD = "HOLD"
    HARD_VETO = "HARD_VETO"


class ExecutionStatus(str, Enum):
    """Execution status — what actually happened, separate from the policy decision.

    Per review §3: the system must not confuse "permitted to execute" with
    "successfully executed."

        PENDING    — Action has not been dispatched yet.
        DISPATCHED  — Action has been sent to the execution system; outcome unknown.
        CONFIRMED  — Execution system confirmed the effect occurred.
        FAILED     — Execution system confirmed no effect occurred.
        UNKNOWN    — Dispatch occurred but outcome is uncertain (timeout, lost response).
                     This is NOT the same as FAILED — the effect may have occurred.
    """
    PENDING = "pending"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CheckType(str, Enum):
    """Whether a check is mandatory or risk-based.

    Per review §1: a mandatory invariant must be checked before every relevant
    effect, regardless of the scoring kernel's output. A risk-based check may
    be selectively invoked if the routing policy is justified.

        MANDATORY  — Must run for every action in scope. Cannot be skipped
                     based on a healthy score. E.g., delegation integrity, budget.
        RISK_BASED — May be selectively invoked or skipped based on routing
                     policy. E.g., expensive cross-object subgraph assembly
                     when the action is proven not to fall within that check's scope.
    """
    MANDATORY = "mandatory"
    RISK_BASED = "risk_based"


# Severity ranking for the decision lattice
_SEVERITY = {
    Decision.PASS: 0,
    Decision.FLAG: 1,
    Decision.HOLD: 2,
    Decision.HARD_VETO: 3,
}