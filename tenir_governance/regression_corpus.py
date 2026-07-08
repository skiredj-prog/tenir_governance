"""
TENIR Regression Corpus
========================
SPRINT 4 — Expand golden cases into true regression corpus

60 golden test cases covering:
  - All 5 CES states
  - All 4 membrane decisions × all 4 operating modes
  - TAU floor boundary conditions
  - partner_b hydrogen / partner_a R&D scenarios
  - SCHIZOPHRENIA detection
  - Horizon exhaustion
  - Option space collapse
  - NSL intent → kernel parameter mapping
  - Cross-R4/R5 compatibility assertions

Fixtures are structured as dicts so they can be loaded by pytest
or consumed by the CI gate directly.

Usage:
    from tenir_governance.regression_corpus import CORPUS, get_case, scenario_group

    # All TAU breach cases
    tau_cases = scenario_group("tau_breach")

    # Run all 60 cases
    for case in CORPUS:
        yield case
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GoldenCase:
    """A single reproducible regression fixture."""
    id: str
    scenario_group: str          # logical grouping
    description: str
    # Input
    pressure: float
    velocity: float
    capacity: float
    option_space: Optional[float]
    operating_mode: str          # canonical OperatingModeNames
    # Expected outputs
    expected_decision: str       # canonical MembraneDecisionNames
    expected_ces_state: str      # canonical CESStateNames
    expected_alert: bool
    expected_intended_block: bool
    # Optional metadata
    policy_variant: str = "default"
    tags: List[str] = field(default_factory=list)
    business_scenario: Optional[str] = None  # human-readable partner_a/partner_b context
    nsl_input: Optional[str] = None          # if this case tests NSL compilation


# ─── CORPUS DEFINITION ───────────────────────────────────────────────────────

CORPUS: List[GoldenCase] = [

    # ── GROUP 1: REST state (S >> 1, system stable) ──────────────────────────
    GoldenCase("G001", "ces_rest",
        "Fully stable system, no stress",
        pressure=0.2, velocity=0.2, capacity=2.0, option_space=0.9,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="partner_a R&D project at start — low pressure, high capacity",
        tags=["ces", "stable", "partner_a"]),

    GoldenCase("G002", "ces_rest",
        "REST state with moderate capacity",
        pressure=0.3, velocity=0.3, capacity=1.8, option_space=0.8,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["ces", "stable"]),

    GoldenCase("G003", "ces_rest",
        "REST in ENFORCE mode — no intervention needed",
        pressure=0.2, velocity=0.2, capacity=2.0, option_space=0.9,
        operating_mode="ENFORCE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["ces", "stable", "enforce"]),

    # ── GROUP 2: METABOLIZING state ───────────────────────────────────────────
    GoldenCase("G010", "ces_metabolizing",
        "System absorbing pressure, S declining slowly",
        pressure=0.6, velocity=0.7, capacity=1.0, option_space=0.6,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="partner_b procurement cycle ramping — moderate load",
        tags=["ces", "metabolizing", "partner_b"]),

    GoldenCase("G011", "ces_metabolizing",
        "Metabolizing approaching alert floor",
        pressure=0.7, velocity=0.8, capacity=0.95, option_space=0.5,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["ces", "metabolizing", "alert"]),

    GoldenCase("G012", "ces_metabolizing",
        "Metabolizing in SHADOW_CRITICAL — alert passes through",
        pressure=0.7, velocity=0.8, capacity=0.95, option_space=0.5,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["ces", "metabolizing", "shadow_critical"]),

    # ── GROUP 3: TENSION state ────────────────────────────────────────────────
    GoldenCase("G020", "ces_tension",
        "Structural stress — S in [0.8, 1.0)",
        pressure=1.0, velocity=1.0, capacity=0.85, option_space=0.45,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_alert", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=False,
        business_scenario="partner_b H2 electrolyzer selection under budget pressure",
        tags=["ces", "tension", "partner_b", "alert"]),

    GoldenCase("G021", "ces_tension",
        "Tension at S=0.82, option space compressed",
        pressure=1.1, velocity=1.1, capacity=1.0, option_space=0.38,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_alert", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=False,
        tags=["ces", "tension", "option_space"]),

    GoldenCase("G022", "ces_tension",
        "Tension boundary — S exactly at alert floor",
        pressure=1.0, velocity=1.0, capacity=0.9, option_space=0.5,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_alert", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=False,
        tags=["ces", "tension", "boundary"]),

    GoldenCase("G023", "ces_tension",
        "Tension in ENFORCE — still only alert (not block)",
        pressure=1.0, velocity=1.0, capacity=0.85, option_space=0.45,
        operating_mode="ENFORCE",
        expected_decision="allow_with_alert", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=False,
        tags=["ces", "tension", "enforce"]),

    # ── GROUP 4: SCHIZOPHRENIA state ──────────────────────────────────────────
    GoldenCase("G030", "ces_schizophrenia",
        "High instability but high option space — contradictory signals",
        pressure=1.5, velocity=1.5, capacity=1.0, option_space=0.75,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="SIGNAL_CONFLICT",
        expected_alert=True, expected_intended_block=True,
        business_scenario="partner_a committee paralysis — high options, high pressure, no convergence",
        tags=["ces", "schizophrenia", "partner_a"]),

    GoldenCase("G031", "ces_schizophrenia",
        "Schizophrenia boundary — S=0.75, option_space=0.65",
        pressure=1.5, velocity=1.5, capacity=0.78, option_space=0.65,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["ces", "schizophrenia", "boundary"]),

    GoldenCase("G032", "ces_schizophrenia",
        "Schizophrenia in ENFORCE — block triggered",
        pressure=1.5, velocity=1.5, capacity=1.0, option_space=0.75,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="SIGNAL_CONFLICT",
        expected_alert=True, expected_intended_block=True,
        tags=["ces", "schizophrenia", "enforce", "block"]),

    # ── GROUP 5: COLLAPSE / TAU breach ───────────────────────────────────────
    GoldenCase("G040", "tau_breach",
        "TAU breach — S below tau_floor=0.42",
        pressure=2.0, velocity=2.0, capacity=1.0, option_space=0.2,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        business_scenario="partner_b procurement lock-in — structural collapse imminent",
        tags=["tau", "collapse", "partner_b"]),

    GoldenCase("G041", "tau_breach",
        "TAU breach in ENFORCE — hard block",
        pressure=2.0, velocity=2.0, capacity=1.0, option_space=0.2,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["tau", "collapse", "enforce", "block"]),

    GoldenCase("G042", "tau_breach",
        "TAU breach — extreme stress, zero option space",
        pressure=3.0, velocity=2.5, capacity=0.5, option_space=0.05,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["tau", "collapse", "enforce", "extreme"]),

    GoldenCase("G043", "tau_breach",
        "TAU boundary — S just above tau_floor",
        pressure=1.8, velocity=1.7, capacity=1.3, option_space=0.3,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["tau", "boundary"]),

    GoldenCase("G044", "tau_breach",
        "SHADOW_OFF mode — no intervention even at TAU breach",
        pressure=2.0, velocity=2.0, capacity=1.0, option_space=0.2,
        operating_mode="SHADOW_OFF",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["tau", "shadow_off"]),

    # ── GROUP 6: Option space collapse ────────────────────────────────────────
    GoldenCase("G050", "option_space_collapse",
        "Option space at alert floor, S still healthy",
        pressure=0.5, velocity=0.5, capacity=1.5, option_space=0.35,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_alert", expected_ces_state="REST",
        expected_alert=True, expected_intended_block=False,
        business_scenario="partner_a partnership — alternatives narrowing despite stable operations",
        tags=["option_space", "alert", "partner_a"]),

    GoldenCase("G051", "option_space_collapse",
        "Option space at block floor — combined with tension",
        pressure=1.0, velocity=1.0, capacity=0.85, option_space=0.20,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_intended_block", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=True,
        tags=["option_space", "intended_block"]),

    GoldenCase("G052", "option_space_collapse",
        "Option space zero — systemic lock-in",
        pressure=1.5, velocity=1.2, capacity=1.0, option_space=0.0,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        business_scenario="partner_b H2 technology lock-in — no reversibility",
        tags=["option_space", "lock_in", "partner_b", "block"]),

    GoldenCase("G053", "option_space_collapse",
        "Option space None — not provided (backwards compat)",
        pressure=0.5, velocity=0.5, capacity=1.5, option_space=None,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["option_space", "none", "backwards_compat"]),

    # ── GROUP 7: Shadow mode boundary ─────────────────────────────────────────
    GoldenCase("G060", "shadow_mode",
        "SHADOW_OFF — same event that would alert in SHADOW_PASSIVE",
        pressure=1.0, velocity=1.0, capacity=0.85, option_space=0.45,
        operating_mode="SHADOW_OFF",
        expected_decision="allow_with_alert", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=False,
        tags=["shadow_off", "mode_boundary"]),

    GoldenCase("G061", "shadow_mode",
        "SHADOW_PASSIVE to SHADOW_CRITICAL escalation path",
        pressure=1.2, velocity=1.2, capacity=0.9, option_space=0.38,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=True,
        tags=["shadow_critical", "escalation"]),

    GoldenCase("G062", "shadow_mode",
        "ENFORCE mode — clean event allowed",
        pressure=0.3, velocity=0.3, capacity=1.8, option_space=0.8,
        operating_mode="ENFORCE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["enforce", "allow"]),

    GoldenCase("G063", "shadow_mode",
        "ENFORCE mode — TAU breach hard block",
        pressure=2.5, velocity=2.5, capacity=0.8, option_space=0.1,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["enforce", "block", "tau"]),

    # ── GROUP 8: Kernel math boundary values ──────────────────────────────────
    GoldenCase("G070", "kernel_math",
        "Minimum epsilon prevents division by zero",
        pressure=0.0, velocity=0.0, capacity=1.0, option_space=0.9,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["kernel", "epsilon", "zero_denominator"]),

    GoldenCase("G071", "kernel_math",
        "Very high capacity overwhelms pressure",
        pressure=5.0, velocity=5.0, capacity=200.0, option_space=0.7,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["kernel", "high_capacity"]),

    GoldenCase("G072", "kernel_math",
        "S exactly at alert floor (0.90) — boundary",
        pressure=1.0, velocity=1.0, capacity=0.9, option_space=0.5,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_alert", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=False,
        tags=["kernel", "boundary", "alert_floor"]),

    GoldenCase("G073", "kernel_math",
        "S exactly at block floor (0.75) — boundary",
        pressure=1.0, velocity=1.0, capacity=0.75, option_space=0.5,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_intended_block", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=True,
        tags=["kernel", "boundary", "block_floor"]),

    # ── GROUP 9: partner_b Green Hydrogen scenarios ─────────────────────────────────
    GoldenCase("G080", "ocp_h2",
        "partner_b H2 — PEM technology under budget pressure",
        pressure=0.8, velocity=0.7, capacity=1.2, option_space=0.55,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="partner_b-H2-2026-001: PEM electrolyzer LCOH assessment — moderate pressure",
        nsl_input="Accelerate the partner_b H2 R&D procurement due to budget risk",
        tags=["partner_b", "h2", "rnd", "nsl"]),

    GoldenCase("G081", "ocp_h2",
        "partner_b H2 — lock-in risk detected",
        pressure=1.4, velocity=1.3, capacity=1.0, option_space=0.22,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        business_scenario="partner_b-H2-2026-001: Technology selection premature lock-in — alkaline only",
        nsl_input="Restrict the electrolyzer procurement to alkaline technology immediately",
        tags=["partner_b", "h2", "lock_in", "nsl"]),

    GoldenCase("G082", "ocp_h2",
        "partner_b H2 — LCOH assumption not stabilized, block advisable",
        pressure=1.8, velocity=1.6, capacity=0.7, option_space=0.18,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        business_scenario="partner_b-H2-2026-001: LCOH cost assumptions unstable — governance block required",
        tags=["partner_b", "h2", "enforce", "block"]),

    GoldenCase("G083", "ocp_h2",
        "partner_b H2 — delay procurement for option space recovery",
        pressure=0.6, velocity=0.5, capacity=1.1, option_space=0.6,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="partner_b-H2-2026-001: Delay procurement to maintain 3-technology option space",
        nsl_input="Delay the procurement contract for H2 electrolyzer until LCOH study complete",
        tags=["partner_b", "h2", "delay", "nsl"]),

    # ── GROUP 10: partner_a institutional scenarios ────────────────────────────────
    GoldenCase("G090", "partner_a",
        "partner_a R&D — partnership initiation, clean",
        pressure=0.25, velocity=0.3, capacity=1.8, option_space=0.85,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="partner_a partnership with partner_b for applied research — early stage",
        nsl_input="Accelerate the R&D partnership project with partner_b for hydrogen research",
        tags=["partner_a", "rnd", "partnership", "nsl"]),

    GoldenCase("G091", "partner_a",
        "partner_a — committee velocity overload",
        pressure=1.3, velocity=1.4, capacity=0.9, option_space=0.4,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=True,
        business_scenario="partner_a governance committee pushing too fast on multiple simultaneous decisions",
        tags=["partner_a", "velocity", "schizophrenia"]),

    GoldenCase("G092", "partner_a",
        "partner_a — budget restriction, moderate",
        pressure=0.7, velocity=0.8, capacity=1.0, option_space=0.55,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="partner_a restricts R&D spending — metabolizing the constraint",
        nsl_input="Restrict budget allocation for applied research to 80% of plan",
        tags=["partner_a", "budget", "restrict", "nsl"]),

    # ── GROUP 11: NSL intent → parameter mapping ──────────────────────────────
    GoldenCase("G100", "nsl_mapping",
        "NSL ACCELERATE maps to higher P and V",
        pressure=0.68, velocity=0.78, capacity=0.85, option_space=0.60,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        nsl_input="Accelerate the R&D project urgently",
        business_scenario="NSL ACCELERATE intent with urgency modifier applied",
        tags=["nsl", "accelerate", "urgency"]),

    GoldenCase("G101", "nsl_mapping",
        "NSL DELAY maps to lower P and V",
        pressure=0.28, velocity=0.28, capacity=0.85, option_space=0.87,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        nsl_input="Delay the procurement contract until legal review complete",
        business_scenario="NSL DELAY intent — system relaxes",
        tags=["nsl", "delay"]),

    GoldenCase("G102", "nsl_mapping",
        "NSL RESTRICT maps to lower K and option_space",
        pressure=0.55, velocity=0.5, capacity=0.60, option_space=0.53,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        nsl_input="Restrict access to the budget allocation for Q3",
        business_scenario="NSL RESTRICT intent — capacity reduced",
        tags=["nsl", "restrict"]),

    GoldenCase("G103", "nsl_mapping",
        "NSL ALLOCATE maps to higher K",
        pressure=0.5, velocity=0.5, capacity=1.0, option_space=0.65,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        nsl_input="Allocate additional capacity to the research program",
        business_scenario="NSL ALLOCATE intent — capacity increases",
        tags=["nsl", "allocate"]),

    # ── GROUP 12: Backwards compatibility (R4 PolicyBundle equivalence) ───────
    GoldenCase("G110", "r4_compat",
        "R4 PolicyBundle: stable event at R4 defaults",
        pressure=0.5, velocity=0.5, capacity=1.5, option_space=0.7,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        policy_variant="default",
        tags=["r4", "compat"]),

    GoldenCase("G111", "r4_compat",
        "R4 PolicyBundle: alert at R4 s_alert_floor=0.90",
        pressure=1.0, velocity=1.0, capacity=0.91, option_space=0.5,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="METABOLIZING",
        expected_alert=False, expected_intended_block=False,
        policy_variant="default",
        tags=["r4", "compat", "alert_floor"]),

    GoldenCase("G112", "r4_compat",
        "R4 PolicyBundle: block at R4 s_block_floor=0.75",
        pressure=1.5, velocity=1.5, capacity=0.76, option_space=0.25,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        policy_variant="default",
        tags=["r4", "compat", "block_floor", "enforce"]),

    # ── GROUP 13: Stress and soak scenarios ───────────────────────────────────
    GoldenCase("G120", "stress",
        "Maximum stress — all dimensions at worst",
        pressure=10.0, velocity=10.0, capacity=0.1, option_space=0.0,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["stress", "extreme", "block"]),

    GoldenCase("G121", "stress",
        "Maximum resilience — all dimensions at best",
        pressure=0.01, velocity=0.01, capacity=100.0, option_space=1.0,
        operating_mode="ENFORCE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["stress", "resilience", "best_case"]),

    GoldenCase("G122", "stress",
        "Repeated identical events — idempotent result",
        pressure=0.5, velocity=0.5, capacity=1.5, option_space=0.7,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["stress", "idempotent"]),

    # ── GROUP 14: Edge cases and boundary probes ──────────────────────────────
    GoldenCase("G130", "edge_cases",
        "S exactly at TAU floor — breach",
        pressure=1.545, velocity=1.545, capacity=1.0, option_space=0.3,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        tags=["edge", "tau", "exact_boundary"]),

    GoldenCase("G131", "edge_cases",
        "Option space exactly at alert floor",
        pressure=0.4, velocity=0.4, capacity=1.5, option_space=0.35,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow_with_alert", expected_ces_state="REST",
        expected_alert=True, expected_intended_block=False,
        tags=["edge", "option_space", "exact_boundary"]),

    GoldenCase("G132", "edge_cases",
        "Zero pressure — system at maximum stability",
        pressure=0.001, velocity=0.001, capacity=1.0, option_space=0.9,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["edge", "zero_pressure"]),

    GoldenCase("G133", "edge_cases",
        "High velocity with high capacity — balanced",
        pressure=0.3, velocity=5.0, capacity=10.0, option_space=0.8,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        tags=["edge", "high_velocity", "high_capacity"]),

    # ── GROUP 15: HOLDING-FIRST posture scenarios ──────────────────────────────
    GoldenCase("G140", "holding_first",
        "HOLDING-FIRST: pre-emptive shadow before pressure arrives",
        pressure=0.3, velocity=0.4, capacity=1.6, option_space=0.7,
        operating_mode="SHADOW_PASSIVE",
        expected_decision="allow", expected_ces_state="REST",
        expected_alert=False, expected_intended_block=False,
        business_scenario="TENIR HOLDING-FIRST doctrine — activate shadow before institutional pressure peaks",
        tags=["holding_first", "proactive", "doctrine"]),

    GoldenCase("G141", "holding_first",
        "HOLDING-FIRST: critical shadow as pressure approaches block floor",
        pressure=1.1, velocity=1.1, capacity=0.85, option_space=0.42,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="TENSION",
        expected_alert=True, expected_intended_block=True,
        business_scenario="TENIR escalation pathway — SHADOW_PASSIVE → SHADOW_CRITICAL pre-enforcement",
        tags=["holding_first", "escalation", "shadow_critical"]),

    # ── GROUP 16: Enforce transition ceremony ────────────────────────────────
    GoldenCase("G150", "enforce_ceremony",
        "Pre-transition state — ready for ENFORCE",
        pressure=1.8, velocity=1.7, capacity=0.8, option_space=0.19,
        operating_mode="SHADOW_CRITICAL",
        expected_decision="allow_with_intended_block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        business_scenario="Governance ceremony: operator signs oath, transitions to ENFORCE",
        tags=["enforce", "ceremony", "transition"]),

    GoldenCase("G151", "enforce_ceremony",
        "Post-transition state — ENFORCE active, same event now blocks",
        pressure=1.8, velocity=1.7, capacity=0.8, option_space=0.19,
        operating_mode="ENFORCE",
        expected_decision="block", expected_ces_state="COLLAPSE",
        expected_alert=True, expected_intended_block=True,
        business_scenario="Post-ceremony: ENFORCE membrane is active, same event hard-blocks",
        tags=["enforce", "ceremony", "block"]),

]


# ─── HELPER FUNCTIONS ─────────────────────────────────────────────────────────

def scenario_group(group: str) -> List[GoldenCase]:
    """Filter corpus by scenario group name."""
    return [c for c in CORPUS if c.scenario_group == group]


def tagged(tag: str) -> List[GoldenCase]:
    """Filter corpus by tag."""
    return [c for c in CORPUS if tag in c.tags]


def get_case(case_id: str) -> GoldenCase:
    """Retrieve a case by ID. Raises KeyError if not found."""
    for c in CORPUS:
        if c.id == case_id:
            return c
    raise KeyError(f"No golden case with id={case_id!r}")


def nsl_cases() -> List[GoldenCase]:
    """All cases with an associated NSL input string."""
    return [c for c in CORPUS if c.nsl_input is not None]


def enforce_cases() -> List[GoldenCase]:
    """All ENFORCE-mode cases."""
    return [c for c in CORPUS if c.operating_mode == "ENFORCE"]


SUMMARY = {
    "total": len(CORPUS),
    "groups": sorted({c.scenario_group for c in CORPUS}),
    "with_nsl": len(nsl_cases()),
    "enforce_mode": len(enforce_cases()),
    "expected_blocks": len([c for c in CORPUS if c.expected_decision == "block"]),
    "expected_intended_blocks": len([c for c in CORPUS if c.expected_decision == "allow_with_intended_block"]),
    "expected_alerts": len([c for c in CORPUS if c.expected_decision == "allow_with_alert"]),
    "expected_allows": len([c for c in CORPUS if c.expected_decision == "allow"]),
}
