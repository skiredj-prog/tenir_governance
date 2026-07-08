"""
TENIR Canonical Nomenclature Registry v2
==========================================
SPRINT 0+6+7+8 — unified vocabulary with plain-language hardening.

Rename-now directives executed (per TENIR_public_safe_lexicon.csv):
  SCHIZOPHRENIA        → SIGNAL_CONFLICT       (alias preserved)
  SCHIZOPHRENIA_ALERT  → SIGNAL_CONFLICT_ALERT (alias preserved)
  WHALE_RESONANCE      → DEEP_PATTERN_SIGNAL   (alias preserved)

Three-register language system:
  CANONICAL — internal doctrine only
  OPERATOR  — UI/API/docs with explicit thresholds
  PUBLIC    — homepage/deck, plain-language, no mythology
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional, Set


class ExposureClass:
    """Language register — enforced by copy_lint."""
    CANONICAL = "canonical"
    OPERATOR  = "operator"
    PUBLIC    = "public"


# ─── EXPANDED POSTURE FAMILY (V5 DOCTRINE) ───────────────────────────────────

class OperatingModeNames:
    """Canonical posture vocabulary (V5 expanded family)."""

    # Core R4/R5 operational modes
    SHADOW_OFF       = "SHADOW_OFF"
    SHADOW_PASSIVE   = "SHADOW_PASSIVE"
    SHADOW_CRITICAL  = "SHADOW_CRITICAL"
    ENFORCE          = "ENFORCE"

    # V5 expanded postures (doctrine: lawful expressions under condition)
    ROAM      = "ROAM"
    LEARN     = "LEARN"
    TRANSMIT  = "TRANSMIT"
    REST      = "REST"
    SLEEP     = "SLEEP"
    STOP      = "STOP"

    ALL_POSTURES: Set[str] = {
        SHADOW_OFF, SHADOW_PASSIVE, SHADOW_CRITICAL, ENFORCE,
        ROAM, LEARN, TRANSMIT, REST, SLEEP, STOP,
    }
    ACTIVE_ADJUDICATION: Set[str] = {
        SHADOW_OFF, SHADOW_PASSIVE, SHADOW_CRITICAL, ENFORCE,
    }
    SUSPENDED_POSTURES: Set[str] = {REST, SLEEP, STOP}

    OPERATOR_LABEL: Dict[str, str] = {
        SHADOW_OFF:      "Observation Off",
        SHADOW_PASSIVE:  "Shadow Monitoring",
        SHADOW_CRITICAL: "Critical Observation",
        ENFORCE:         "Active Governance",
        ROAM:            "Open Exploration",
        LEARN:           "Pattern Acquisition",
        TRANSMIT:        "Insight Transmission",
        REST:            "Reduced Activity",
        SLEEP:           "Deep Suspension",
        STOP:            "Hard Halt",
    }

    PUBLIC_LABEL: Dict[str, str] = {
        SHADOW_OFF:      "Not monitoring",
        SHADOW_PASSIVE:  "Watching — won't block actions",
        SHADOW_CRITICAL: "Watching closely — will warn",
        ENFORCE:         "Guarding — will block risky actions",
        ROAM:            "Exploring",
        LEARN:           "Studying patterns",
        TRANSMIT:        "Sharing conclusions",
        REST:            "Resting",
        SLEEP:           "Offline",
        STOP:            "Halted",
    }

    # Backward-compat alias (some older code still reads BUSINESS_ALIAS)
    BUSINESS_ALIAS: Dict[str, str] = OPERATOR_LABEL

    R4_TO_R5: Dict[str, str] = {
        "SHADOW_OFF":      SHADOW_OFF,
        "SHADOW_PASSIVE":  SHADOW_PASSIVE,
        "SHADOW_CRITICAL": SHADOW_CRITICAL,
        "ENFORCE":         ENFORCE,
    }


# ─── KERNEL MATH NAMES ───────────────────────────────────────────────────────

class KernelFieldNames:
    PRESSURE     = "pressure"
    VELOCITY     = "velocity"
    CAPACITY     = "capacity"
    OPTION_SPACE = "option_space"

    STABILITY                = "stability"
    DS_DE                    = "ds_de"
    D2S_DE2                  = "d2s_de2"
    PROJECTED_EVENTS_TO_ZERO = "projected_events_to_zero"
    OPTION_SPACE_LOW         = "option_space_low"
    ALERT                    = "alert"
    INTENDED_BLOCK           = "intended_block"
    RATIONALE                = "rationale"

    TAU_FLOOR         = "tau_floor"
    S_ALERT_FLOOR     = "s_alert_floor"
    S_BLOCK_FLOOR     = "s_block_floor"
    DS_DE_ALERT_FLOOR = "ds_de_alert_floor"
    EPSILON           = "epsilon"

    OPERATOR_LABEL: Dict[str, str] = {
        "stability":                "Stability Index",
        "ds_de":                    "Stability Velocity",
        "d2s_de2":                  "Stability Acceleration",
        "projected_events_to_zero": "Decision Horizon (events)",
        "option_space":             "Remaining Viable Choices",
        "alert":                    "Warning Signal",
        "intended_block":           "Block Signal",
        "tau_floor":                "Minimum Operating Integrity",
    }
    PUBLIC_LABEL: Dict[str, str] = {
        "stability":    "Structural stability",
        "ds_de":        "Whether stability is rising or falling",
        "option_space": "How many viable paths remain",
        "tau_floor":    "Safety threshold",
    }
    BUSINESS_ALIAS: Dict[str, str] = OPERATOR_LABEL


# ─── CES STATE NAMES — RECONCILED WITH DOCTRINE ──────────────────────────────

class CESStateNames:
    """Combinatorial Entity State — SIGNAL_CONFLICT rename applied."""

    REST            = "REST"
    METABOLIZING    = "METABOLIZING"
    TENSION         = "TENSION"
    SIGNAL_CONFLICT = "SIGNAL_CONFLICT"   # formerly SCHIZOPHRENIA
    COLLAPSE        = "COLLAPSE"

    # Backward-compat alias (ledger re-read)
    SCHIZOPHRENIA = SIGNAL_CONFLICT

    LEGACY_ALIASES: Dict[str, str] = {
        "SCHIZOPHRENIA":       SIGNAL_CONFLICT,
        "SCHIZOPHRENIA_ALERT": f"{SIGNAL_CONFLICT}_ALERT",
    }

    @classmethod
    def normalize(cls, raw: str) -> str:
        return cls.LEGACY_ALIASES.get(raw, raw)

    ALL: Set[str] = {REST, METABOLIZING, TENSION, SIGNAL_CONFLICT, COLLAPSE}

    OPERATOR_LABEL: Dict[str, str] = {
        REST:            "Stable — no action needed",
        METABOLIZING:    "Absorbing pressure — monitor",
        TENSION:         "Under stress — warning raised",
        SIGNAL_CONFLICT: "Contradictory signals — escalate",
        COLLAPSE:        "Integrity failure — intervene now",
    }

    PUBLIC_LABEL: Dict[str, str] = {
        REST:            "System is quiet",
        METABOLIZING:    "System is working and handling it",
        TENSION:         "System is stressed",
        SIGNAL_CONFLICT: "Signals disagree — need to resolve",
        COLLAPSE:        "System has crossed a safety threshold",
    }

    BUSINESS_ALIAS: Dict[str, str] = OPERATOR_LABEL

    RECOMMENDED_POSTURE: Dict[str, str] = {
        REST:            OperatingModeNames.SHADOW_PASSIVE,
        METABOLIZING:    OperatingModeNames.SHADOW_PASSIVE,
        TENSION:         OperatingModeNames.SHADOW_CRITICAL,
        SIGNAL_CONFLICT: OperatingModeNames.SHADOW_CRITICAL,
        COLLAPSE:        OperatingModeNames.ENFORCE,
    }


# ─── MEMBRANE DECISION NAMES ─────────────────────────────────────────────────

class MembraneDecisionNames:
    ALLOW                      = "allow"
    ALLOW_WITH_ALERT           = "allow_with_alert"
    ALLOW_WITH_INTENDED_BLOCK  = "allow_with_intended_block"
    BLOCK                      = "block"

    OPERATOR_LABEL: Dict[str, str] = {
        "allow":                     "Admitted — proceed",
        "allow_with_alert":          "Admitted — warning raised",
        "allow_with_intended_block": "Admitted (would block in Active mode)",
        "block":                     "Blocked by governance layer",
    }
    PUBLIC_LABEL: Dict[str, str] = {
        "allow":                     "Accepted",
        "allow_with_alert":          "Accepted with warning",
        "allow_with_intended_block": "Under review",
        "block":                     "Blocked",
    }
    BUSINESS_ALIAS: Dict[str, str] = OPERATOR_LABEL
    REQUIRES_ADVISORY: Set[str] = {ALLOW_WITH_ALERT, ALLOW_WITH_INTENDED_BLOCK, BLOCK}
    BLOCKING: Set[str] = {BLOCK}


# ─── NSL FIELD NAMES ─────────────────────────────────────────────────────────

class NSLFieldNames:
    INTENT      = "intent"
    ENTITY_TYPE = "entity_type"
    ENTITY_ID   = "entity_identifier"
    MODIFIERS   = "modifiers"
    CONTEXT     = "context"
    CONFIDENCE  = "confidence"
    BACKEND     = "backend"
    LATENCY_MS  = "latency_ms"

    ACCELERATE = "ACCELERATE"
    DELAY      = "DELAY"
    RESTRICT   = "RESTRICT"
    ALLOCATE   = "ALLOCATE"
    QUERY      = "QUERY"

    RND_PROJECT          = "RND_PROJECT"
    PROCUREMENT_CONTRACT = "PROCUREMENT_CONTRACT"
    BUDGET               = "BUDGET"
    PERSONNEL            = "PERSONNEL"
    LEGAL_POLICY         = "LEGAL_POLICY"

    INTENT_OPERATOR_LABEL: Dict[str, str] = {
        "ACCELERATE": "Accelerate workflow",
        "DELAY":      "Defer workflow",
        "RESTRICT":   "Constrain workflow",
        "ALLOCATE":   "Allocate resources",
        "QUERY":      "Status inquiry",
    }
    INTENT_BUSINESS_ALIAS: Dict[str, str] = INTENT_OPERATOR_LABEL


# ─── WEBSOCKET FRAME TYPES (RENAMES APPLIED) ─────────────────────────────────

class WSFrameTypes:
    TRAJECTORY_UPDATE     = "TRAJECTORY_UPDATE"
    CES_STATE_CHANGE      = "CES_STATE_CHANGE"
    LEDGER_APPEND         = "LEDGER_APPEND"
    MODE_TRANSITION       = "MODE_TRANSITION"
    SIGNAL_CONFLICT_ALERT = "SIGNAL_CONFLICT_ALERT"   # formerly SCHIZOPHRENIA_ALERT
    TAU_BREACH            = "TAU_BREACH"
    DEEP_PATTERN_SIGNAL   = "DEEP_PATTERN_SIGNAL"     # formerly WHALE_RESONANCE
    CONNECTED             = "CONNECTED"
    PONG                  = "PONG"
    ERROR                 = "ERROR"

    REQUEST_TRANSITION = "REQUEST_TRANSITION"
    OPERATOR_NOTE      = "OPERATOR_NOTE"
    PING               = "PING"

    LEGACY_ALIASES: Dict[str, str] = {
        "SCHIZOPHRENIA_ALERT": SIGNAL_CONFLICT_ALERT,
        "WHALE_RESONANCE":     DEEP_PATTERN_SIGNAL,
    }

    @classmethod
    def normalize(cls, raw: str) -> str:
        return cls.LEGACY_ALIASES.get(raw, raw)


# ─── LEDGER ENTRY TYPES ──────────────────────────────────────────────────────

class LedgerEntryTypes:
    OBSERVATION             = "observation"
    CONTROL_TRANSITION      = "control_transition"
    UNAUTHORIZED_TRANSITION = "unauthorized_transition_attempt"
    MANUAL_OVERRIDE         = "manual_override"
    DISSENT                 = "dissent"
    TESTAMENT               = "testament"
    EPOCH_SEAL              = "epoch_seal"
    PEER_CONSENSUS          = "peer_consensus_result"
    POSTURE_CHANGE          = "posture_change"


# ─── CAVE MATRIX (CANONICAL, LOCKED) ─────────────────────────────────────────

class CAVEFieldNames:
    """CAVE = Context · Action · Value · Effect. NOT Control/Aim/Veto/Epistemics."""
    CONTEXT = "context"
    ACTION  = "action"
    VALUE   = "value"
    EFFECT  = "effect"
    ACRONYM   = "CAVE"
    EXPANSION = "Context · Action · Value · Effect"
    FORBIDDEN_EXPANSIONS: Set[str] = {
        "Control · Aim · Veto · Epistemics",
        "Control/Aim/Veto/Epistemics",
    }


# ─── ADMISSIBILITY CLASSES (V5 SYMPTOMATIC LAYER) ────────────────────────────

class AdmissibilityClass:
    OBSERVED  = "observed"
    DERIVED   = "derived"
    INFERRED  = "inferred"
    HELD      = "held"
    FORBIDDEN = "forbidden"

    ALL: Set[str] = {OBSERVED, DERIVED, INFERRED, HELD, FORBIDDEN}

    TRUST_WEIGHT: Dict[str, float] = {
        OBSERVED:  1.00,
        DERIVED:   0.95,
        INFERRED:  0.50,
        HELD:      0.00,
        FORBIDDEN: 0.00,
    }


# ─── INSTITUTIONAL NAMES ─────────────────────────────────────────────────────

class InstitutionalNames:
    partner_a  = "partner_a"
    partner_b   = "partner_b"
    TENIR = "TENIR"

    UM6P_ROLE  = "opens the space through experimentation, pedagogy, and impact"
    OCP_ROLE   = "hardens the stakes through consequence, scale, and accountability"
    TENIR_ROLE = "governs the passage through dignity, bounded agency, and invariant logic"


# ─── COPY-LINT BANNED TERMS ──────────────────────────────────────────────────

PUBLIC_BANNED_TERMS: Set[str] = {
    "canon", "canonical",
    "doctrine", "doctrinal",
    "estate",
    "constitutional", "constitutional core",
    "sovereign node",
    "membrane",
    "incarnation",
    "architecte-stratège", "architecte-stratege",
    "epistemic sovereignty",
    "metabolic profile",
    "rhizomatic",
    "schizophrenia", "SCHIZOPHRENIA",
    "whale resonance", "WHALE_RESONANCE",
    "la baleine",
}

PUBLIC_TRANSLATE_ON_FIRST_USE: Set[str] = {
    "TAU", "TENIR", "CAVE", "NSL", "Phoenix",
    "Lens", "Diagnostic",
    "shadow mode", "enforce mode", "TAU breach",
}


# ─── TERM REGISTRY ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CanonicalTerm:
    kernel_name: str
    protocol_name: str
    operator_label: str
    public_label: str
    exposure: str
    source_doc: str


TERM_REGISTRY: Dict[str, CanonicalTerm] = {
    "stability": CanonicalTerm(
        "stability", "s_score", "Stability Index", "Structural stability",
        ExposureClass.OPERATOR, "SPEC-101 / V42CIron RC"),
    "tau_floor": CanonicalTerm(
        "tau_floor", "tau_floor", "Minimum Operating Integrity", "Safety threshold",
        ExposureClass.OPERATOR, "V42CIron RC"),
    "shadow_passive": CanonicalTerm(
        "SHADOW_PASSIVE", "SHADOW", "Shadow Monitoring", "Watching — won't block actions",
        ExposureClass.OPERATOR, "Regroup v5"),
    "enforce": CanonicalTerm(
        "ENFORCE", "ENFORCE", "Active Governance", "Guarding — will block risky actions",
        ExposureClass.OPERATOR, "Regroup v5"),
    "signal_conflict": CanonicalTerm(
        "SIGNAL_CONFLICT", "SIGNAL_CONFLICT",
        "Contradictory signals — escalate", "Signals disagree",
        ExposureClass.OPERATOR, "Translation Audit 2026-04"),
    "deep_pattern_signal": CanonicalTerm(
        "DEEP_PATTERN_SIGNAL", "DEEP_PATTERN_SIGNAL",
        "High-salience pattern detected", "Unusual pattern detected",
        ExposureClass.OPERATOR, "Translation Audit 2026-04"),
    "cave_context": CanonicalTerm(
        "context", "cave_context", "Situational context", "What's happening",
        ExposureClass.OPERATOR, "TENIR_ABCD_Canonical_Map"),
    "cave_action": CanonicalTerm(
        "action", "cave_action", "Proposed action", "What we're about to do",
        ExposureClass.OPERATOR, "TENIR_ABCD_Canonical_Map"),
    "cave_value": CanonicalTerm(
        "value", "cave_value", "Stake / principle at risk", "What we could lose",
        ExposureClass.OPERATOR, "TENIR_ABCD_Canonical_Map"),
    "cave_effect": CanonicalTerm(
        "effect", "cave_effect", "Projected consequence", "What will likely follow",
        ExposureClass.OPERATOR, "TENIR_ABCD_Canonical_Map"),
    "membrane_block": CanonicalTerm(
        "block", "block", "Blocked by governance layer", "Blocked",
        ExposureClass.OPERATOR, "V42CIron RC"),
}


# ─── PHOENIX RESILIENCE TAXONOMY ──────────────────────────────────────────────

class PhoenixResilienceTaxonomy:
    """Project Phoenix UI projection taxonomy for Lens18."""
    TRANSLATION_MAP: Dict[str, str] = {
        CESStateNames.REST: "Équilibre Phoenix",
        CESStateNames.METABOLIZING: "Assimilation Phoenix",
        CESStateNames.TENSION: "Tension Phoenix",
        CESStateNames.SIGNAL_CONFLICT: "Dissonance Phoenix",
        CESStateNames.COLLAPSE: "Phase de décompression : Ancrage nécessaire.",
    }

def phoenix_label(ces_state: str) -> str:
    """Returns the Phoenix UI projection for a CES state."""
    return PhoenixResilienceTaxonomy.TRANSLATION_MAP.get(ces_state, "État en cours de traitement")

# ─── HELPER FUNCTIONS ────────────────────────────────────────────────────────

def business_label(kernel_name: str, layer_class: type = KernelFieldNames) -> str:
    if layer_class == CESStateNames or kernel_name in CESStateNames.ALL:
        return phoenix_label(kernel_name)
    return getattr(layer_class, "OPERATOR_LABEL",
                   getattr(layer_class, "BUSINESS_ALIAS", {})).get(kernel_name, kernel_name)

def public_label(kernel_name: str, layer_class: type) -> str:
    return getattr(layer_class, "PUBLIC_LABEL", {}).get(kernel_name, kernel_name)

def r4_to_r5_mode(r4_mode: str) -> str:
    return OperatingModeNames.R4_TO_R5.get(r4_mode, r4_mode)

def normalize_ces_state(raw: str) -> str:
    return CESStateNames.normalize(raw)

def normalize_ws_frame_type(raw: str) -> str:
    return WSFrameTypes.normalize(raw)
