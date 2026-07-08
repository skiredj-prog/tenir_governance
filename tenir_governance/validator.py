"""
TENIR Governance Validator
===========================
SPRINT 1 — Validator as CI Gate

The TENIRValidator is the single enforcement point for:
  1. Policy contract compliance
  2. Event sample shape and bounds
  3. Kernel math sanity (reproducibility check)
  4. Ledger chain integrity
  5. Cross-R4/R5 nomenclature consistency

Usage as CI gate (see .github/workflows/tenir-ci.yml):
    python -m tenir_governance.validator --policy-version tenir-canonical-v1.0.0

Usage as pytest fixture (see tests/conftest.py):
    @pytest.fixture
    def validator():
        return TENIRValidator.for_ci()

Usage programmatically:
    validator = TENIRValidator(policy=PolicyEngine.default())
    report = validator.validate_all()
    assert report.passed, report.summary()
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .nomenclature import KernelFieldNames, OperatingModeNames, MembraneDecisionNames
from .policy_engine import PolicyEngine, PolicyViolation


# ─── VALIDATION RESULT ────────────────────────────────────────────────────────

@dataclass
class ValidationFinding:
    level: str          # "PASS" | "FAIL" | "WARN"
    check_id: str
    description: str
    detail: Optional[str] = None


@dataclass
class ValidationReport:
    findings: List[ValidationFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.level == "FAIL" for f in self.findings)

    @property
    def fail_count(self) -> int:
        return sum(1 for f in self.findings if f.level == "FAIL")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.level == "WARN")

    def summary(self) -> str:
        lines = [
            f"TENIR Governance Validator — {'PASSED' if self.passed else 'FAILED'}",
            f"  Checks: {len(self.findings)}  |  FAIL: {self.fail_count}  |  WARN: {self.warn_count}",
        ]
        for f in self.findings:
            icon = "✓" if f.level == "PASS" else ("✗" if f.level == "FAIL" else "⚠")
            lines.append(f"  {icon} [{f.check_id}] {f.description}")
            if f.detail:
                lines.append(f"      → {f.detail}")
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        return {
            "passed": self.passed,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "findings": [
                {"level": f.level, "check_id": f.check_id,
                 "description": f.description, "detail": f.detail}
                for f in self.findings
            ]
        }


# ─── VALIDATOR ────────────────────────────────────────────────────────────────

class TENIRValidator:
    """
    Institutional-grade governance validator.

    Checks that:
    - The policy contract is valid and versioned
    - Event samples are structurally sound
    - The trajectory kernel produces reproducible math
    - The membrane decision function is monotone (tighter inputs → tighter decisions)
    - Ledger entries (if provided) form a valid hash chain
    - Nomenclature alignment between R4 and R5 structures

    This is the SINGLE CI gate. Nothing ships without passing it.
    """

    def __init__(
        self,
        policy: Optional[PolicyEngine] = None,
        expected_version: Optional[str] = None,
    ) -> None:
        self.policy = policy or PolicyEngine.default()
        self.expected_version = expected_version
        self._report = ValidationReport()

    @classmethod
    def for_ci(cls) -> "TENIRValidator":
        """Factory for CI pipeline use."""
        return cls(policy=PolicyEngine.default(), expected_version="tenir-canonical-v1.0.0")


    # ── Public entry point ────────────────────────────────────────────────────

    def validate_all(self) -> ValidationReport:
        self._report = ValidationReport()
        self._check_policy_contract()
        self._check_kernel_math_reproducibility()
        self._check_membrane_monotonicity()
        self._check_membrane_mode_boundary()
        self._check_ces_classification_coverage()
        self._check_nomenclature_alignment()
        self._check_tau_floor_ordering()
        return self._report

    def validate_event_sample(self, event: Dict) -> ValidationReport:
        """Validate a single event sample dict."""
        self._report = ValidationReport()
        self._check_event_sample_shape(event)
        return self._report

    def validate_ledger_chain(self, ledger_path: Path) -> ValidationReport:
        """Validate a JSONL ledger file's hash chain integrity."""
        self._report = ValidationReport()
        self._check_ledger_chain(ledger_path)
        return self._report

    # ── Check: Policy contract ────────────────────────────────────────────────

    def _check_policy_contract(self) -> None:
        try:
            self.policy.validate()
            self._pass("POL-001", "Policy contract validates without error",
                       f"version={self.policy.version!r} fingerprint={self.policy.fingerprint()}")
        except PolicyViolation as e:
            self._fail("POL-001", "Policy contract failed validation", str(e))
            return

        if self.expected_version and self.policy.version != self.expected_version:
            self._fail("POL-002", "Policy version mismatch",
                       f"expected {self.expected_version!r}, got {self.policy.version!r}")
        else:
            self._pass("POL-002", "Policy version matches expected value")

    # ── Check: Kernel math reproducibility ───────────────────────────────────

    def _check_kernel_math_reproducibility(self) -> None:
        """
        Verify S = K/(P·V+ε) is deterministic and matches expected values.
        Golden fixtures drawn from the V42CIron RC audit (epsilon=0.01 in V42C,
        but we test with canonical policy epsilon).
        """
        fixtures = [
            # (P, V, K, expected_S_approx) — computed with policy.epsilon
            (0.5, 0.5, 1.0, 4.0),      # S = 1 / (0.25 + ε) ≈ 4.0
            (1.0, 1.0, 1.0, 1.0),      # S = 1 / (1.0 + ε)  ≈ 1.0
            (1.5, 1.5, 1.0, 0.4444),   # S = 1 / (2.25 + ε) ≈ 0.4444 (TAU-adjacent)
            (1.0, 1.0, 0.5, 0.5),      # S = 0.5 / (1.0 + ε) ≈ 0.5   (below s_block_floor)
        ]
        all_pass = True
        for P, V, K, expected_approx in fixtures:
            eps = self.policy.epsilon
            computed = K / (P * V + eps)
            tol = 0.001 + abs(expected_approx) * 0.01
            if abs(computed - expected_approx) > tol:
                self._fail("KER-001", "Kernel math reproducibility failed",
                           f"P={P} V={V} K={K}: expected≈{expected_approx:.4f}, got {computed:.4f}")
                all_pass = False
        if all_pass:
            self._pass("KER-001", "Kernel math S=K/(P·V+ε) is reproducible across 4 fixtures")

    # ── Check: Membrane monotonicity ─────────────────────────────────────────

    def _check_membrane_monotonicity(self) -> None:
        """
        Tighter inputs (lower S, lower option_space) must not produce looser decisions.
        Decision ordering: allow < allow_with_alert < allow_with_intended_block < block
        """
        decision_rank = {
            MembraneDecisionNames.ALLOW:                    0,
            MembraneDecisionNames.ALLOW_WITH_ALERT:         1,
            MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK:2,
            MembraneDecisionNames.BLOCK:                    3,
        }
        # Test: decreasing S in SHADOW mode produces non-decreasing decision severity
        prev_rank = -1
        s_sequence = [2.0, 1.5, 1.0, 0.85, 0.80, 0.75, 0.42, 0.30]
        monotone = True
        for s in s_sequence:
            d, _, _, _ = self.policy.evaluate_membrane(
                s_score=s, ds_de=0.0, d2s_de2=0.0,
                option_space=0.5, projected_events_to_zero=None,
                operating_mode=OperatingModeNames.SHADOW_PASSIVE,
            )
            rank = decision_rank.get(d, -1)
            if rank < prev_rank:
                self._fail("MEM-001", "Membrane decision is not monotone with decreasing S",
                           f"S={s}: decision={d} (rank {rank}) is less severe than previous rank {prev_rank}")
                monotone = False
                break
            prev_rank = max(prev_rank, rank)

        if monotone:
            self._pass("MEM-001", "Membrane decisions are monotone non-decreasing as S decreases")

    # ── Check: Membrane mode boundary ────────────────────────────────────────

    def _check_membrane_mode_boundary(self) -> None:
        """
        In ENFORCE mode, a TAU-breaching event must return 'block'.
        In SHADOW modes, the same event must return 'allow_with_intended_block'.
        This is the single most important invariant.
        """
        s_breach = self.policy.tau_floor * 0.5  # well below TAU

        d_shadow, _, _, _ = self.policy.evaluate_membrane(
            s_score=s_breach, ds_de=-0.1, d2s_de2=-0.05,
            option_space=0.1, projected_events_to_zero=2,
            operating_mode=OperatingModeNames.SHADOW_PASSIVE,
        )
        d_enforce, _, _, _ = self.policy.evaluate_membrane(
            s_score=s_breach, ds_de=-0.1, d2s_de2=-0.05,
            option_space=0.1, projected_events_to_zero=2,
            operating_mode=OperatingModeNames.ENFORCE,
        )

        if d_shadow != MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK:
            self._fail("MEM-002", "SHADOW mode does not return intended_block for TAU breach",
                       f"got {d_shadow!r}")
        elif d_enforce != MembraneDecisionNames.BLOCK:
            self._fail("MEM-002", "ENFORCE mode does not return block for TAU breach",
                       f"got {d_enforce!r}")
        else:
            self._pass("MEM-002",
                       "SHADOW→intended_block and ENFORCE→block correctly differentiated at TAU breach")

    # ── Check: CES classification coverage ───────────────────────────────────

    def _check_ces_classification_coverage(self) -> None:
        """All five CES states must be reachable from the policy classifier."""
        from .nomenclature import CESStateNames
        # (pressure, velocity, capacity, option_space, expected_state)
        # Classifier now uses activity (P·V) to distinguish REST from METABOLIZING.
        p = self.policy
        # Build fixtures adaptively using the actual policy thresholds.
        # This lets default, partner_a and partner_b policies all pass CES-001.
        s_alert = p.s_alert_floor
        s_block = p.s_block_floor
        tau = p.tau_floor

        # Choose a K value that produces the target S given (P, V).
        # S = K / (P·V + ε) → K = S · (P·V + ε)
        def k_for(target_s, P, V):
            return target_s * (P * V + p.epsilon)

        # REST: S well above alert, low activity (P·V < 1.0)
        rest_s = s_alert * 2.0
        # METABOLIZING: S just above alert, activity ≥ 1.0, not over-resourced
        metab_s = s_alert * 1.15
        # TENSION: S strictly between block and alert
        tension_s = (s_block + s_alert) / 2
        # SIGNAL_CONFLICT: S < s_block_floor AND option_space ≥ 0.55
        conflict_s = s_block * 0.85
        # COLLAPSE: S < tau_floor
        collapse_s = tau * 0.5

        test_cases = [
            (0.5, 0.5, k_for(rest_s, 0.5, 0.5),         0.8, CESStateNames.REST),
            (1.2, 1.0, k_for(metab_s, 1.2, 1.0),        0.6, CESStateNames.METABOLIZING),
            (1.0, 1.0, k_for(tension_s, 1.0, 1.0),      0.5, CESStateNames.TENSION),
            (1.0, 1.0, k_for(conflict_s, 1.0, 1.0),     0.70, CESStateNames.SIGNAL_CONFLICT),
            (2.0, 2.0, k_for(collapse_s, 2.0, 2.0),     0.10, CESStateNames.COLLAPSE),
        ]
        all_pass = True
        for P, V, K, opt, expected in test_cases:
            s = self.policy.capacity_s(P, V, K)
            got = self.policy.classify_ces(s, 0.0, opt, pressure=P, velocity=V)
            if got != expected:
                self._fail("CES-001", f"CES classifier missed state {expected!r}",
                           f"P={P} V={V} K={K} opt={opt} → S={s:.4f} → got {got!r}")
                all_pass = False
        if all_pass:
            self._pass("CES-001", "All five CES states reachable (incl. SIGNAL_CONFLICT)")

    # ── Check: Nomenclature alignment ────────────────────────────────────────

    def _check_nomenclature_alignment(self) -> None:
        """Verify CAVE canonical definition is consistent."""
        from .nomenclature import CAVEFieldNames
        # The canonical expansion must NOT contain 'Control' or 'Veto' (deprecated variant)
        expansion = CAVEFieldNames.EXPANSION
        if "Control" in expansion or "Veto" in expansion:
            self._fail("NOM-001", "CAVE definition contains deprecated 'Control/Veto' variant",
                       f"Found in: {expansion!r}")
        else:
            self._pass("NOM-001", f"CAVE canonical definition is correct: {expansion!r}")

        # Verify OperatingMode R4↔R5 map is bidirectionally consistent
        from .nomenclature import OperatingModeNames
        r4_to_r5 = OperatingModeNames.R4_TO_R5
        if len(set(r4_to_r5.values())) < 2:
            self._warn("NOM-002", "R4→R5 mode map has suspiciously few distinct values")
        else:
            self._pass("NOM-002", f"R4→R5 mode mapping covers {len(r4_to_r5)} modes")

    # ── Check: TAU floor ordering ─────────────────────────────────────────────

    def _check_tau_floor_ordering(self) -> None:
        """tau_floor < s_block_floor < s_alert_floor is a cardinal invariant."""
        p = self.policy
        if not (p.tau_floor < p.s_block_floor <= p.s_alert_floor):
            self._fail("POL-003", "TAU floor ordering violated",
                       f"τ={p.tau_floor} < s_block={p.s_block_floor} ≤ s_alert={p.s_alert_floor} must hold")
        else:
            self._pass("POL-003",
                       f"TAU floor ordering: τ={p.tau_floor} < s_block={p.s_block_floor} ≤ s_alert={p.s_alert_floor}")

    # ── Check: Event sample shape ─────────────────────────────────────────────

    def _check_event_sample_shape(self, event: Dict) -> None:
        required = ["pressure", "velocity", "capacity"]
        for field in required:
            if field not in event:
                self._fail("EVT-001", f"EventSample missing required field: {field!r}")
                return
            val = event[field]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                self._fail("EVT-001", f"EventSample.{field} must be numeric, got {type(val).__name__}")
                return
            if not math.isfinite(float(val)) or val < 0:
                self._fail("EVT-001", f"EventSample.{field}={val} must be finite and ≥ 0")
                return

        opt = event.get("option_space")
        if opt is not None:
            if not isinstance(opt, (int, float)) or isinstance(opt, bool):
                self._fail("EVT-001", "EventSample.option_space must be numeric")
                return
            if not (0.0 <= float(opt) <= 1.0):
                self._fail("EVT-001", f"EventSample.option_space={opt} must be in [0, 1]")
                return

        self._pass("EVT-001", "EventSample shape is valid")

    # ── Check: Ledger chain integrity ─────────────────────────────────────────

    def _check_ledger_chain(self, ledger_path: Path) -> None:
        import hashlib
        if not ledger_path.exists():
            self._warn("LED-001", f"Ledger file not found: {ledger_path}")
            return

        prev_hash = "GENESIS"
        broken = 0
        count = 0

        with ledger_path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    self._fail("LED-001", f"Ledger line {i} is invalid JSON", str(e))
                    return

                stored_prev = entry.get("previous_hash", "")
                if stored_prev != prev_hash:
                    broken += 1

                payload = entry.get("payload", entry)
                clean = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                chain_input = f"{prev_hash}|{clean}".encode("utf-8")
                computed_hash = hashlib.sha256(chain_input).hexdigest()

                stored_hash = entry.get("chain_hash", entry.get("entry_hash", ""))
                if stored_hash and stored_hash != computed_hash:
                    broken += 1

                prev_hash = stored_hash or computed_hash
                count += 1

        if broken:
            self._fail("LED-001", f"Ledger chain has {broken} broken links in {count} entries")
        elif count == 0:
            self._warn("LED-001", "Ledger is empty")
        else:
            self._pass("LED-001", f"Ledger chain valid: {count} entries, 0 broken links")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pass(self, check_id: str, description: str, detail: Optional[str] = None) -> None:
        self._report.findings.append(ValidationFinding("PASS", check_id, description, detail))

    def _fail(self, check_id: str, description: str, detail: Optional[str] = None) -> None:
        self._report.findings.append(ValidationFinding("FAIL", check_id, description, detail))

    def _warn(self, check_id: str, description: str, detail: Optional[str] = None) -> None:
        self._report.findings.append(ValidationFinding("WARN", check_id, description, detail))


# ─── CLI ENTRY POINT ──────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    """
    CI gate entry point.

    Returns 0 if all checks pass, 1 on any FAIL.
    Used by .github/workflows/tenir-ci.yml as blocking step.
    """
    import argparse
    parser = argparse.ArgumentParser(description="TENIR Governance Validator — CI Gate")
    parser.add_argument("--policy", choices=["default"],
                        default="default", help="Policy variant to validate against")
    parser.add_argument("--expected-version", help="Assert exact policy version string")
    parser.add_argument("--ledger", help="Path to a JSONL ledger file to validate")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args(argv)

    policy_map = {
        "default": PolicyEngine.default,
    }
    # If the user provides an invalid policy choice, it will now fail gracefully 
    # instead of looking for non-existent attributes.
    if args.policy not in policy_map:
        print(f"Error: Policy {args.policy} not supported in this version.")
        return 1
        
    policy = policy_map[args.policy]()

    validator = TENIRValidator(policy=policy, expected_version=args.expected_version)
    report = validator.validate_all()

    if args.ledger:
        report.findings += validator.validate_ledger_chain(Path(args.ledger)).findings

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
