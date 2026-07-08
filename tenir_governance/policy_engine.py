"""
TENIR PolicyEngine
==================
SPRINT 1 — Policy Engine + Validator Merge

The single, importable governance policy contract.
Both R4 (TenirMonitor) and R5 (r5_server.py) must import their
policy decisions from HERE — not from local ad-hoc threshold checks.

Design principle (from Gap Register, item G2):
  The adjudication outcome is only trustworthy if the policy
  that produced it is cryptographically versioned and validated
  before any kernel evaluation begins.

The governance runtime MUST NOT evaluate events unless:
  1. policy.validate() passes
  2. policy.assert_version_matches(EXPECTED_VERSION) passes
  3. the policy instance is sealed (immutable after construction)

Usage:
    from tenir_governance.policy_engine import PolicyEngine, PolicyViolation

    policy = PolicyEngine.default()
    policy.validate()  # raises PolicyViolation if invalid

    # Explicit membrane decision — both R4 and R5 use this:
    decision = policy.evaluate_membrane(trajectory_state, operating_mode)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .nomenclature import (
    CESStateNames,
    KernelFieldNames,
    MembraneDecisionNames,
    OperatingModeNames,
)


# ─── ERRORS ──────────────────────────────────────────────────────────────────

class PolicyViolation(ValueError):
    """Raised when a PolicyEngine fails its invariant checks."""


# ─── POLICY ENGINE ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyEngine:
    """
    Versioned, immutable policy contract for the TENIR governance membrane.

    Replaces:
      - R4's PolicyBundle (tenir_v4_test/models.py)
      - R5's ad-hoc threshold checks in r5_server.py
      - Any embedded threshold literals in test fixtures

    Both R4 and R5 governance runtimes depend on this class explicitly.
    No governance decision may be produced without a validated PolicyEngine.

    Policy version format: <scope>-<variant>-<semver>
    Example: "tenir-partner_a-shadow-v4-1.0.0"
    """

    # Identity
    version: str = "tenir-canonical-v1.0.0"
    scope: str = "canonical"           

    # TAU invariant (canonical: V42CIron origin)
    tau_floor: float = 0.42

    # Kernel thresholds (S = K / (P·V + ε))
    epsilon: float = 1e-6
    s_alert_floor: float = 0.90             # S below here → advisory
    s_block_floor: float = 0.75             # S below here → intended block

    # Derivative thresholds
    ds_de_alert_floor: float = -0.05       # dS/de below here → alert
    d2s_de2_alert_floor: float = -0.03     # d²S/de² below here → alert

    # Horizon thresholds
    reaction_budget_events: int = 5        # events until zero → alert
    event_window: int = 8                  # stability history window

    # Option space thresholds
    option_space_alert_floor: float = 0.35
    option_space_block_floor: float = 0.20

    # NSL confidence gating (R5)
    nsl_confidence_threshold: float = 0.82  # below → fall back to grammar

    # Epoch size (R5 distributed ledger)
    epoch_size: int = 100


    # ── Validation ────────────────────────────────────────────────────────────

    def validate(self) -> None:
        """
        Full policy invariant check. Must pass before any governance evaluation.
        Raises PolicyViolation (subclass of ValueError) on failure.
        """
        self._check_version()
        self._check_floats()
        self._check_integers()
        self._check_ordering()

    def _check_version(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise PolicyViolation("policy.version must be a non-empty string")
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise PolicyViolation("policy.scope must be a non-empty string")

    def _check_floats(self) -> None:
        import math
        float_fields = {
            "epsilon":               (self.epsilon,               1e-15, None),
            "s_alert_floor":         (self.s_alert_floor,         0.0,   None),
            "s_block_floor":         (self.s_block_floor,         0.0,   None),
            "tau_floor":             (self.tau_floor,             0.0,   1.0),
            "ds_de_alert_floor":     (self.ds_de_alert_floor,     None,  0.0),
            "d2s_de2_alert_floor":   (self.d2s_de2_alert_floor,   None,  0.0),
            "option_space_alert_floor": (self.option_space_alert_floor, 0.0, 1.0),
            "option_space_block_floor": (self.option_space_block_floor, 0.0, 1.0),
            "nsl_confidence_threshold": (self.nsl_confidence_threshold, 0.0, 1.0),
        }
        for name, (value, minimum, maximum) in float_fields.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PolicyViolation(f"{name} must be a real number")
            if not math.isfinite(float(value)):
                raise PolicyViolation(f"{name} must be finite")
            if minimum is not None and value < minimum:
                raise PolicyViolation(f"{name} must be >= {minimum}")
            if maximum is not None and value > maximum:
                raise PolicyViolation(f"{name} must be <= {maximum}")

    def _check_integers(self) -> None:
        if not isinstance(self.event_window, int) or self.event_window < 3:
            raise PolicyViolation("event_window must be an integer >= 3")
        if not isinstance(self.reaction_budget_events, int) or self.reaction_budget_events < 1:
            raise PolicyViolation("reaction_budget_events must be an integer >= 1")
        if not isinstance(self.epoch_size, int) or self.epoch_size < 10:
            raise PolicyViolation("epoch_size must be an integer >= 10")

    def _check_ordering(self) -> None:
        if self.s_block_floor > self.s_alert_floor:
            raise PolicyViolation("s_block_floor must be <= s_alert_floor")
        if self.option_space_block_floor > self.option_space_alert_floor:
            raise PolicyViolation("option_space_block_floor must be <= option_space_alert_floor")
        if self.tau_floor >= self.s_block_floor:
            raise PolicyViolation("tau_floor must be < s_block_floor")


    # ── Membrane Decision ─────────────────────────────────────────────────────

    def evaluate_membrane(
        self,
        s_score: float,
        ds_de: float,
        d2s_de2: float,
        option_space: Optional[float],
        projected_events_to_zero: Optional[int],
        operating_mode: str,
    ) -> Tuple[str, str, bool, bool]:
        """
        Central membrane decision.

        Returns:
            (membrane_decision, rationale, alert, intended_block)

        Both R4 TenirMonitor and R5 adjudication endpoint call this.
        No local threshold checks permitted outside this method.
        """
        alert_reasons: List[str] = []
        block_reasons: List[str] = []

        # ── Alert conditions ──────────────────────────────────────────────────
        if s_score <= self.s_alert_floor:
            alert_reasons.append(f"S={s_score:.4f} ≤ alert floor {self.s_alert_floor}")
        if ds_de <= self.ds_de_alert_floor:
            alert_reasons.append(f"dS/de={ds_de:.4f} ≤ alert floor {self.ds_de_alert_floor}")
        if d2s_de2 <= self.d2s_de2_alert_floor:
            alert_reasons.append(f"d²S/de²={d2s_de2:.4f} ≤ alert floor {self.d2s_de2_alert_floor}")
        if (projected_events_to_zero is not None
                and projected_events_to_zero <= self.reaction_budget_events):
            alert_reasons.append(
                f"horizon={projected_events_to_zero}ev ≤ reaction budget {self.reaction_budget_events}ev"
            )
        if option_space is not None and option_space <= self.option_space_alert_floor:
            alert_reasons.append(f"option_space={option_space:.3f} ≤ alert floor {self.option_space_alert_floor}")

        # ── Block conditions ──────────────────────────────────────────────────
        if s_score <= self.s_block_floor:
            block_reasons.append(f"S={s_score:.4f} ≤ block floor {self.s_block_floor}")
        if s_score <= self.tau_floor:
            block_reasons.append(f"TAU BREACH: S={s_score:.4f} ≤ τ={self.tau_floor}")
        if (projected_events_to_zero is not None
                and projected_events_to_zero <= max(1, self.reaction_budget_events // 2)):
            block_reasons.append(f"collapse horizon critically near ({projected_events_to_zero}ev)")
        if option_space is not None and option_space <= self.option_space_block_floor:
            block_reasons.append(f"option_space={option_space:.3f} ≤ block floor {self.option_space_block_floor}")

        alert = bool(alert_reasons)
        intended_block = bool(block_reasons)

        # ── Membrane decision ─────────────────────────────────────────────────
        if not alert and not intended_block:
            decision = MembraneDecisionNames.ALLOW
            rationale = "System stable. TAU preserved. All thresholds clear."
        elif intended_block:
            if operating_mode == OperatingModeNames.ENFORCE:
                decision = MembraneDecisionNames.BLOCK
                rationale = f"Blocked (ENFORCE): {'; '.join(block_reasons)}"
            else:
                decision = MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK
                rationale = f"Intended block (SHADOW): {'; '.join(block_reasons)}"
        else:  # alert only
            decision = MembraneDecisionNames.ALLOW_WITH_ALERT
            rationale = f"Advisory: {'; '.join(alert_reasons)}"

        return decision, rationale, alert, intended_block


    # ── CES Classification ────────────────────────────────────────────────────

    def classify_ces(
        self,
        s_score: float,
        ds_de: float,
        option_space: Optional[float],
        pressure: Optional[float] = None,
        velocity: Optional[float] = None,
    ) -> str:
        """
        Map kernel outputs to Combinatorial Entity State — doctrine-reconciled.

        Semantics (per TENIR_Master_Doctrine_Note_v5_Regroup.md):

          COLLAPSE          S < tau_floor, OR
                            S ≤ s_block_floor AND option_space ≤ 0.30
                            (identity-threatening breach, no viable alternatives)

          SIGNAL_CONFLICT   S < s_alert_floor AND option_space ≥ 0.55
                            (materially contradictory pressures — multiple viable
                             but incompatible paths; formerly 'SCHIZOPHRENIA')

          TENSION           s_block_floor ≤ S < s_alert_floor, OR
                            S ≤ s_block_floor AND 0.30 < option_space < 0.55
                            (structural stress, one dominant voice)

          METABOLIZING      S ≥ s_alert_floor AND activity ≥ 1.00
                            (actively absorbing high pressure)

          REST              S ≥ s_alert_floor AND activity < 1.00
                            (reserve preservation, low activity)

        Where activity = pressure × velocity.
        If P or V unavailable, defaults to REST above alert floor.
        """
        os_val = option_space if option_space is not None else 1.0

        # TAU breach dominates
        if s_score < self.tau_floor:
            return CESStateNames.COLLAPSE

        # Below block floor — partition by option_space width
        if s_score <= self.s_block_floor:
            if os_val <= 0.30:
                return CESStateNames.COLLAPSE
            if os_val >= 0.55:
                return CESStateNames.SIGNAL_CONFLICT
            return CESStateNames.TENSION

        # Between block and alert floors — TENSION dominates,
        # but very high option_space + declining S → SIGNAL_CONFLICT
        if s_score < self.s_alert_floor:
            if os_val >= 0.70 and s_score < (self.s_block_floor + self.s_alert_floor) / 2:
                return CESStateNames.SIGNAL_CONFLICT
            return CESStateNames.TENSION

        # Above alert floor — REST vs METABOLIZING by activity AND margin
        # Doctrine: METABOLIZING = "absorbing pressure".
        # If S is very high (≥ 2.5 × s_alert_floor), the system has abundant
        # reserve; even with activity, the system is not metabolizing stress.
        very_high_s = s_score >= 2.5 * self.s_alert_floor
        if not very_high_s and pressure is not None and velocity is not None:
            activity = pressure * velocity
            if activity >= 1.00:   # doctrine: real metabolic pressure
                return CESStateNames.METABOLIZING
        return CESStateNames.REST


    # ── Fingerprint ──────────────────────────────────────────────────────────

    def fingerprint(self) -> str:
        """SHA-256 of the canonical JSON serialization of this policy."""
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def assert_version_matches(self, expected_version: str) -> None:
        """Governance runtime guard: raises PolicyViolation if version drifts."""
        if self.version != expected_version:
            raise PolicyViolation(
                f"Policy version mismatch: expected {expected_version!r}, "
                f"got {self.version!r}. Governance runtime cannot proceed."
            )


    # ── Factory Methods ───────────────────────────────────────────────────────

    @classmethod
    def default(cls) -> "PolicyEngine":
        """Returns the canonical default policy."""
        p = cls()
        p.validate()
        return p
  

    def to_r4_policy_bundle(self) -> dict:
        """
        Export this policy as R4 PolicyBundle-compatible dict.
        Used during the R4→R5 migration window.
        """
        return {
            "version": self.version,
            "epsilon": self.epsilon,
            "s_alert_floor": self.s_alert_floor,
            "s_block_floor": self.s_block_floor,
            "ds_de_alert_floor": self.ds_de_alert_floor,
            "d2s_de2_alert_floor": self.d2s_de2_alert_floor,
            "reaction_budget_events": self.reaction_budget_events,
            "event_window": self.event_window,
            "option_space_alert_floor": self.option_space_alert_floor,
            "option_space_block_floor": self.option_space_block_floor,
        }


    def capacity_s(self, pressure: float, velocity: float, capacity: float) -> float:
        """Compute S = K / (P·V + ε). Central kernel math."""
        return capacity / (pressure * velocity + self.epsilon)
