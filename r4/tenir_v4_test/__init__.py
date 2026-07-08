"""TENIR internal v4 test line for partner_a shadow-mode UAT."""

from .adjudication import (
    DIMENSION_WEIGHTS,
    LOOP_STAGES,
    SCHEMA_VERSION,
    PilotValidationError,
    build_pilot_payload,
    evidence_gate_for,
    normalize_raw_score,
    sample_pilot_template,
    validate_pilot_payload,
)
from .control_auth import OperatorRegistry, TransitionProof
from .controller import TenirMonitor
from .ledger import HashChainedLedger, LedgerIntegrityError, LedgerLockError
from .models import (
    BurnEstimate,
    BurnInputs,
    EventSample,
    OperatingMode,
    PolicyBundle,
    TrajectoryState,
    Verdict,
)
from .overlay_burn import estimate_burn_cost

__all__ = [
    "TenirMonitor",
    "PilotValidationError",
    "OperatorRegistry",
    "TransitionProof",
    "HashChainedLedger",
    "LedgerIntegrityError",
    "LedgerLockError",
    "SCHEMA_VERSION",
    "DIMENSION_WEIGHTS",
    "LOOP_STAGES",
    "BurnInputs",
    "BurnEstimate",
    "EventSample",
    "OperatingMode",
    "PolicyBundle",
    "TrajectoryState",
    "Verdict",
    "estimate_burn_cost",
    "normalize_raw_score",
    "evidence_gate_for",
    "build_pilot_payload",
    "validate_pilot_payload",
    "sample_pilot_template",
]
