"""
TENIR Polymorphic Surface State Contract
==========================================
SPRINT 10 — V5 Persona/Workflow Architecture

Per TENIR_V5_Persona_Workflow_Architecture.pdf and Master Doctrine v5:
  "The product family is a single Polymorphic Adjudication Surface
   with variable opacity, capable of morphing in real-time based on
   the required workflow and the user's role."

Four surface states (doctrine-locked):
  AMBIENT       — monitoring / metabolism view (Cockpit)
  ANTICIPATION  — simulation / ghost trajectories (Chambre d'Anticipation)
  ADJUDICATION  — crisis / override (high-contrast cryptographic demand)
  FORENSIC      — auditing / raw ledger + NSL grammar (Console)

Two-Glass Strategy:
  IRON  — backend/architect view (kernel, cryptography, NSL)
  GLASS — frontend/sovereign view (living adjudication surface)

The Bridge:
  Machine proposes. Human disposes.
  All IRON↔GLASS transitions carry cryptographic sign-offs.

This module is the single source of truth for:
  - Which surface state is active given the current system condition
  - Which persona classes are authorized for each state
  - Which transitions between states are admissible
  - What data contract each state receives from the governance runtime
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .nomenclature import (
    CESStateNames,
    MembraneDecisionNames,
    OperatingModeNames,
    AdmissibilityClass,
)


# ─── SURFACE STATES (the four faces) ─────────────────────────────────────────

class SurfaceState:
    """Canonical Polymorphic Surface state names."""

    AMBIENT       = "AMBIENT"        # Cockpit — monitoring, biological view
    ANTICIPATION  = "ANTICIPATION"   # Chambre d'Anticipation — simulation
    ADJUDICATION  = "ADJUDICATION"   # crisis/override — high contrast, demanding
    FORENSIC      = "FORENSIC"       # Console — raw ledger, NSL grammar, Merkle

    ALL: Set[str] = {AMBIENT, ANTICIPATION, ADJUDICATION, FORENSIC}

    # Operator-register labels
    OPERATOR_LABEL: Dict[str, str] = {
        AMBIENT:       "Monitoring Cockpit",
        ANTICIPATION:  "Simulation Chamber",
        ADJUDICATION:  "Crisis Override",
        FORENSIC:      "Forensic Console",
    }

    # Public-register labels (plain language, no mythology)
    PUBLIC_LABEL: Dict[str, str] = {
        AMBIENT:       "Watching the system",
        ANTICIPATION:  "Testing a decision before committing",
        ADJUDICATION:  "Handling a critical decision",
        FORENSIC:      "Reviewing the audit trail",
    }


# ─── PERSONAS (Two-Glass Strategy) ───────────────────────────────────────────

class Persona:
    """Canonical persona classes with role-based access."""

    # IRON personas (backend view)
    SYSTEM_ARCHITECT     = "SYSTEM_ARCHITECT"
    GOVERNANCE_ENGINEER  = "GOVERNANCE_ENGINEER"
    LEAD_AUDITOR         = "LEAD_AUDITOR"

    # GLASS personas (frontend view)
    EXECUTIVE            = "EXECUTIVE"
    BUSINESS_LEADER      = "BUSINESS_LEADER"
    OPERATIONAL_MANAGER  = "OPERATIONAL_MANAGER"

    IRON:  Set[str] = {SYSTEM_ARCHITECT, GOVERNANCE_ENGINEER, LEAD_AUDITOR}
    GLASS: Set[str] = {EXECUTIVE, BUSINESS_LEADER, OPERATIONAL_MANAGER}
    ALL:   Set[str] = IRON | GLASS

    OPERATOR_LABEL: Dict[str, str] = {
        SYSTEM_ARCHITECT:     "System Architect",
        GOVERNANCE_ENGINEER:  "Governance Engineer",
        LEAD_AUDITOR:         "Lead Auditor",
        EXECUTIVE:            "Executive",
        BUSINESS_LEADER:      "Business Leader",
        OPERATIONAL_MANAGER:  "Operational Manager",
    }


# ─── SURFACE ACCESS POLICY ───────────────────────────────────────────────────
# Which persona classes may view / act on each surface state.

SURFACE_ACCESS: Dict[str, Dict[str, Set[str]]] = {
    SurfaceState.AMBIENT: {
        "view": Persona.ALL,              # Everyone can monitor
        "act":  Persona.ALL,              # All can inject events in observation
    },
    SurfaceState.ANTICIPATION: {
        "view": Persona.ALL,              # Simulation is open to all
        "act":  Persona.ALL,              # Ghost trajectories don't hit the ledger
    },
    SurfaceState.ADJUDICATION: {
        "view": Persona.ALL,              # All may witness the ceremony
        "act":  Persona.IRON              # Only IRON personas may override
                | {Persona.EXECUTIVE},    # ... plus Executive for business block
    },
    SurfaceState.FORENSIC: {
        "view": Persona.IRON | {Persona.EXECUTIVE},   # Audit is restricted
        "act":  {Persona.LEAD_AUDITOR},               # Only auditors can annotate
    },
}


# ─── SURFACE STATE RECOMMENDER ───────────────────────────────────────────────

@dataclass(frozen=True)
class SurfaceContext:
    """Input to the surface state recommender."""
    operating_mode: str          # OperatingModeNames
    membrane_decision: str       # MembraneDecisionNames
    ces_state: str               # CESStateNames
    persona: str                 # Persona.*
    simulation_active: bool = False       # user has opened ghost trajectory mode
    audit_requested: bool = False         # user has requested audit trail view


def recommend_surface_state(ctx: SurfaceContext) -> str:
    """
    Given the current governance runtime state, return the surface state
    the UI should morph into.

    Precedence (most urgent wins):
      1. If membrane_decision == block or intended_block → ADJUDICATION
      2. If audit_requested → FORENSIC
      3. If simulation_active → ANTICIPATION
      4. Otherwise → AMBIENT
    """
    # ── 1. ADJUDICATION — crisis or block ────────────────────────────────────
    if ctx.membrane_decision in (
        MembraneDecisionNames.BLOCK,
        MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK,
    ):
        return SurfaceState.ADJUDICATION

    if ctx.ces_state == CESStateNames.COLLAPSE:
        return SurfaceState.ADJUDICATION

    # ── 2. FORENSIC — auditing requested ─────────────────────────────────────
    if ctx.audit_requested:
        return SurfaceState.FORENSIC

    # ── 3. ANTICIPATION — simulation active ──────────────────────────────────
    if ctx.simulation_active:
        return SurfaceState.ANTICIPATION

    # ── 4. AMBIENT — default monitoring ──────────────────────────────────────
    return SurfaceState.AMBIENT


# ─── TRANSITION LEGALITY ─────────────────────────────────────────────────────

# From any state, which transitions are lawful?
# AMBIENT ↔ ANTICIPATION: free (simulation doesn't hit ledger)
# AMBIENT → ADJUDICATION: automatic on crisis (no confirmation needed)
# ADJUDICATION → AMBIENT: requires cryptographic sign-off (the ceremony)
# Any → FORENSIC: requires audit permission
# FORENSIC → elsewhere: free (read-only state)

_LAWFUL_TRANSITIONS: Set[Tuple[str, str]] = {
    (SurfaceState.AMBIENT,       SurfaceState.ANTICIPATION),
    (SurfaceState.ANTICIPATION,  SurfaceState.AMBIENT),
    (SurfaceState.AMBIENT,       SurfaceState.ADJUDICATION),
    (SurfaceState.ANTICIPATION,  SurfaceState.ADJUDICATION),
    (SurfaceState.ADJUDICATION,  SurfaceState.AMBIENT),
    (SurfaceState.ADJUDICATION,  SurfaceState.FORENSIC),
    (SurfaceState.AMBIENT,       SurfaceState.FORENSIC),
    (SurfaceState.ANTICIPATION,  SurfaceState.FORENSIC),
    (SurfaceState.FORENSIC,      SurfaceState.AMBIENT),
    (SurfaceState.FORENSIC,      SurfaceState.ANTICIPATION),
    (SurfaceState.FORENSIC,      SurfaceState.ADJUDICATION),
}


def is_lawful_transition(from_state: str, to_state: str) -> bool:
    """Return True iff the transition is in the lawful set."""
    if from_state == to_state:
        return True
    return (from_state, to_state) in _LAWFUL_TRANSITIONS


def requires_ceremony(from_state: str, to_state: str) -> bool:
    """
    Return True if this transition requires a cryptographic ceremony
    (HMAC-signed operator oath).

    Doctrine: moving OUT of ADJUDICATION back to AMBIENT requires ceremony,
    because the operator is implicitly asserting the crisis is resolved.
    """
    return from_state == SurfaceState.ADJUDICATION and to_state == SurfaceState.AMBIENT


# ─── DATA CONTRACT PER STATE ─────────────────────────────────────────────────

@dataclass(frozen=True)
class SurfaceFrame:
    """
    The data contract passed from the governance runtime to the UI surface.

    Structured so R5's WebSocket hub can serialize directly into this shape.
    """
    state: str                              # SurfaceState.*
    operating_mode: str                     # OperatingModeNames
    persona: str                            # Persona.*
    policy_version: str
    policy_fingerprint: str

    # Kernel state (optional — AMBIENT and FORENSIC need full detail)
    s_score: Optional[float] = None
    ds_de: Optional[float] = None
    d2s_de2: Optional[float] = None
    option_space: Optional[float] = None
    ces_state: Optional[str] = None
    membrane_decision: Optional[str] = None

    # Ceremony state (ADJUDICATION only)
    ceremony_required: bool = False
    ceremony_nonce: Optional[str] = None

    # Forensic detail (FORENSIC only)
    ledger_entries: List[Dict] = field(default_factory=list)
    chain_hash: Optional[str] = None

    # Simulation state (ANTICIPATION only)
    ghost_trajectory: Optional[Dict] = None

    # Admissibility trace (all states) — V5 symptomatic layer
    admissibility_trace: Dict[str, str] = field(default_factory=dict)


def build_surface_frame(
    state: str,
    operating_mode: str,
    persona: str,
    policy_version: str,
    policy_fingerprint: str,
    **kwargs: Any,
) -> SurfaceFrame:
    """
    Factory that enforces the per-state data contract.

    For each surface state, only the relevant fields are populated; others
    are left at their defaults. This makes serialization to the WebSocket
    frame type unambiguous.
    """
    if state not in SurfaceState.ALL:
        raise ValueError(f"Unknown surface state: {state!r}")

    if persona not in Persona.ALL:
        raise ValueError(f"Unknown persona: {persona!r}")

    # Enforce persona access for the requested state
    if persona not in SURFACE_ACCESS[state]["view"]:
        raise PermissionError(
            f"Persona {persona!r} not authorized for surface state {state!r}"
        )

    return SurfaceFrame(
        state=state,
        operating_mode=operating_mode,
        persona=persona,
        policy_version=policy_version,
        policy_fingerprint=policy_fingerprint,
        **kwargs,
    )


# ─── OPACITY MAP (for the "variable opacity" doctrine) ───────────────────────
# Each state has a default visual opacity that morphs smoothly.
# 0.0 = transparent (AMBIENT breathing), 1.0 = full-contrast (ADJUDICATION)

STATE_OPACITY: Dict[str, float] = {
    SurfaceState.AMBIENT:       0.35,
    SurfaceState.ANTICIPATION:  0.55,
    SurfaceState.ADJUDICATION:  1.00,
    SurfaceState.FORENSIC:      0.85,
}


# ─── BRIDGE — IRON↔GLASS TRANSITION AUTHORITY ────────────────────────────────

def can_cross_bridge(
    persona: str,
    direction: str,   # "iron_to_glass" | "glass_to_iron"
) -> bool:
    """
    The Iron↔Glass bridge is cryptographically gated.
    Only specific personas may cross in each direction.
    """
    if direction == "iron_to_glass":
        # IRON persona viewing GLASS surfaces — generally permitted
        return persona in Persona.IRON
    if direction == "glass_to_iron":
        # GLASS persona crossing INTO IRON — Executive only, with ceremony
        return persona == Persona.EXECUTIVE
    raise ValueError(f"Unknown bridge direction: {direction!r}")


# ─── EXPORT ──────────────────────────────────────────────────────────────────

__all__ = [
    "SurfaceState",
    "Persona",
    "SURFACE_ACCESS",
    "SurfaceContext",
    "recommend_surface_state",
    "is_lawful_transition",
    "requires_ceremony",
    "SurfaceFrame",
    "build_surface_frame",
    "STATE_OPACITY",
    "can_cross_bridge",
]
