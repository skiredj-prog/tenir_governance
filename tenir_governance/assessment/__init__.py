"""Post-execution assessment layer — τ⁻¹ (partial tau inversion) and
EVP (intended value preservation).

Both concepts are **ex-post** (post-execution): they run AFTER the dispatch
state machine reaches a terminal state (CONFIRMED or FAILED), never before.

Critical doctrine constraint: neither τ⁻¹ nor EVP is an input to the
admissibility function D. They measure consequences of governance discipline,
not gates for it. High expected value never justifies an otherwise
inadmissible transition.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
It is NOT canonical, NOT doctrinal. It carries implementation-level
governance enforcement authority only.
"""
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
from .assessor import (
    PostExecutionAssessor,
    AssessmentReport,
)

__all__ = [
    # τ⁻¹
    "TauInversionClassifier",
    "TauInversionResult",
    "InversionClassification",
    # EVP
    "EVPAssessor",
    "EVPReport",
    "EVPGross",
    "EVPNet",
    "FrictionCost",
    "GovernanceDebt",
    # Assessor
    "PostExecutionAssessor",
    "AssessmentReport",
]