# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Composition layer — decision lattice, governor, and schema adapter.

This package provides the two-kernel composition layer that bridges
the continuous risk monitor (TenirKernel) with the discrete invariant
enforcer (TransitionMembrane).

Exports:
    Decision, ExecutionStatus, CheckType — implementation-level enums
    CompositionPolicy — configurable policy for the composition lattice
    compose — the decision lattice function
    HashChainedLedger, LedgerEntry — tamper-evident evidence ledger
    DualKernelGovernor — the composed two-kernel governor
    ScoringKernelAdapter — bridges TenirKernel to the governor's scoring interface
    conform_to_schema — maps governor output to TENIR 2.0 schema 1.2
    POSTURE_MAP, EXECUTION_DISPOSITION_MAP — KB3 posture mappings
"""
from .decisions import Decision, ExecutionStatus, CheckType
from .policy import CompositionPolicy
from .lattice import compose, HashChainedLedger, LedgerEntry
from .governor import DualKernelGovernor, ScoringKernelAdapter
from .hold_strategy import CompositionHoldStrategy, CompositionHoldManager
from .schema_adapter import (
    conform_to_schema,
    POSTURE_MAP,
    EXECUTION_DISPOSITION_MAP,
)
from .._profile_meta import DISCLOSURE

__all__ = [
    "Decision", "ExecutionStatus", "CheckType", "CompositionPolicy",
    "compose", "HashChainedLedger", "LedgerEntry",
    "DualKernelGovernor", "ScoringKernelAdapter",
    "CompositionHoldStrategy", "CompositionHoldManager",
    "conform_to_schema", "POSTURE_MAP", "EXECUTION_DISPOSITION_MAP",
    "DISCLOSURE",
]
