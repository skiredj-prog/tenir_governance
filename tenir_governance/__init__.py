"""
TENIR Governance Package
========================
Institutional-grade governance runtime for TENIR 2C.

Public API (Sprint 2 — hardened package boundaries):
  All external consumers import from THIS module only.
  Sub-module internals are not part of the public contract.

Governance Branch:    tenir_governance/
├── nomenclature.py          — Sprint 0: canonical term registry (R4+R5 unified)
├── policy_engine.py         — Sprint 1: policy contract + membrane decision
├── validator.py             — Sprint 1/3: CI gate (standalone + pytest fixture)
├── regression_corpus.py     — Sprint 4: 281 golden cases
├── sdk.py                   — Sprint 5: SDK/Governance branch public client
├── polymorphic_surface.py   — Sprint 10: V5 surface state contract
├── copy_lint.py             — Sprint 9: public-safe lexicon enforcement
└── ledger_migrate.py        — Sprint 11: legacy label migration

Version: 5.0.0 (IRON OMEGA R5)
Policy: tenir-canonical-v1.0.0
Policy fingerprint: d083e0b82a16c04d
"""

from .nomenclature import (
    OperatingModeNames,
    KernelFieldNames,
    CESStateNames,
    MembraneDecisionNames,
    NSLFieldNames,
    WSFrameTypes,
    LedgerEntryTypes,
    CAVEFieldNames,
    InstitutionalNames,
    AdmissibilityClass,
    ExposureClass,
    TERM_REGISTRY,
    PUBLIC_BANNED_TERMS,
    PUBLIC_TRANSLATE_ON_FIRST_USE,
    business_label,
    public_label,
    r4_to_r5_mode,
    normalize_ces_state,
    normalize_ws_frame_type,
)

from .policy_engine import PolicyEngine, PolicyViolation

from .validator import TENIRValidator, ValidationReport, ValidationFinding

from .sdk import TENIRGovernanceClient, GovernanceEvent, GovernanceResult

from .regression_corpus import (
    CORPUS,
    GoldenCase,
    scenario_group,
    tagged,
    get_case,
    SUMMARY as CORPUS_SUMMARY,
)

from .polymorphic_surface import (
    SurfaceState,
    Persona,
    SurfaceContext,
    SurfaceFrame,
    recommend_surface_state,
    is_lawful_transition,
    requires_ceremony,
    build_surface_frame,
    can_cross_bridge,
    STATE_OPACITY,
    SURFACE_ACCESS,
)

from .copy_lint import CopyLinter, LintReport as CopyLintReport, Finding as CopyLintFinding
from .ledger_migrate import migrate_ledger, verify_migrated_ledger, MigrationReport

__version__ = "5.0.0"
__policy_version__ = "tenir-canonical-v1.0.0"

__all__ = [
    # Nomenclature
    "OperatingModeNames", "KernelFieldNames", "CESStateNames",
    "MembraneDecisionNames", "NSLFieldNames", "WSFrameTypes",
    "LedgerEntryTypes", "CAVEFieldNames", "InstitutionalNames",
    "AdmissibilityClass", "ExposureClass",
    "TERM_REGISTRY", "PUBLIC_BANNED_TERMS", "PUBLIC_TRANSLATE_ON_FIRST_USE",
    "business_label", "public_label", "r4_to_r5_mode",
    "normalize_ces_state", "normalize_ws_frame_type",
    # Policy
    "PolicyEngine", "PolicyViolation",
    # Validator
    "TENIRValidator", "ValidationReport", "ValidationFinding",
    # SDK
    "TENIRGovernanceClient", "GovernanceEvent", "GovernanceResult",
    # Corpus
    "CORPUS", "GoldenCase", "scenario_group", "tagged", "get_case",
    "CORPUS_SUMMARY",
    # Polymorphic Surface (V5)
    "SurfaceState", "Persona", "SurfaceContext", "SurfaceFrame",
    "recommend_surface_state", "is_lawful_transition", "requires_ceremony",
    "build_surface_frame", "can_cross_bridge",
    "STATE_OPACITY", "SURFACE_ACCESS",
    # Tooling
    "CopyLinter", "CopyLintReport", "CopyLintFinding",
    "migrate_ledger", "verify_migrated_ledger", "MigrationReport",
]
