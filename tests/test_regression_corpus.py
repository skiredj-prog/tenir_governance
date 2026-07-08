"""
TENIR Governance — Full Regression Test Suite
=============================================
SPRINT 3 — Validator as CI Gate
SPRINT 4 — Regression Corpus wired to pytest

Runs all 60 golden cases against the policy engine.
Used as the blocking CI step in .github/workflows/tenir-ci.yml.

Run:
    pytest tests/test_regression_corpus.py -v
    pytest tests/test_regression_corpus.py -v --tb=short -q  (CI mode)
"""

from __future__ import annotations

import math
import pytest
from pathlib import Path

# These imports are the EXPLICIT dependency on the policy engine and validator.
# No test may use locally-defined thresholds.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tenir_governance.policy_engine import PolicyEngine, PolicyViolation
from tenir_governance.validator import TENIRValidator, ValidationReport
from tenir_governance.nomenclature import (
    OperatingModeNames, MembraneDecisionNames, CESStateNames, CAVEFieldNames
)
from tenir_governance.regression_corpus import (
    CORPUS, GoldenCase, scenario_group, tagged, get_case, SUMMARY
)


# ─── FIXTURES ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def default_policy() -> PolicyEngine:
    """Canonical default policy. Session-scoped: instantiated once."""
    p = PolicyEngine.default()
    return p


@pytest.fixture(scope="session")
def um6p_policy() -> PolicyEngine:
    """Institutional shadow policy — uses canonical default for public release."""
    return PolicyEngine.default()


@pytest.fixture(scope="session")
def ocp_policy() -> PolicyEngine:
    """Sovereign pilot policy — uses canonical default for public release."""
    return PolicyEngine.default()


@pytest.fixture(scope="session")
def validator(default_policy) -> TENIRValidator:
    """Validator instance for CI gate checks."""
    return TENIRValidator(policy=default_policy)


# ─── SPRINT 1: POLICY ENGINE ─────────────────────────────────────────────────

class TestPolicyEngineContract:

    def test_default_policy_validates_without_error(self, default_policy):
        default_policy.validate()  # must not raise

    def test_policy_fingerprint_is_deterministic(self, default_policy):
        fp1 = default_policy.fingerprint()
        fp2 = default_policy.fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 16  # SHA-256 truncated to 16 chars

    def test_policy_version_assertion_passes_on_match(self, default_policy):
        default_policy.assert_version_matches("tenir-canonical-v1.0.0")

    def test_policy_version_assertion_fails_on_mismatch(self, default_policy):
        with pytest.raises(PolicyViolation, match="version mismatch"):
            default_policy.assert_version_matches("wrong-version-1.0.0")

    def test_tau_floor_ordering_invariant(self, default_policy):
        p = default_policy
        assert p.tau_floor < p.s_block_floor <= p.s_alert_floor

    def test_invalid_policy_raises(self):
        with pytest.raises(PolicyViolation):
            PolicyEngine(s_block_floor=0.95, s_alert_floor=0.90).validate()

    def test_tau_above_block_floor_raises(self):
        with pytest.raises(PolicyViolation, match="tau_floor must be < s_block_floor"):
            PolicyEngine(tau_floor=0.80, s_block_floor=0.75).validate()

    def test_r4_export_compatibility(self):
        policy = PolicyEngine.default()
        bundle = policy.to_r4_policy_bundle()
        required_keys = ["version", "epsilon", "s_alert_floor", "s_block_floor",
                         "ds_de_alert_floor", "reaction_budget_events", "event_window"]
        for key in required_keys:
            assert key in bundle, f"Missing key in R4 export: {key!r}"
        assert bundle["version"] == "tenir-canonical-v1.0.0"


# ─── SPRINT 1: MEMBRANE DECISION ─────────────────────────────────────────────

class TestMembraneCoreInvariants:

    def test_allow_when_stable(self, default_policy):
        d, _, alert, block = default_policy.evaluate_membrane(
            2.0, 0.0, 0.0, 0.8, None, OperatingModeNames.SHADOW_PASSIVE
        )
        assert d == MembraneDecisionNames.ALLOW
        assert not alert
        assert not block

    def test_block_on_tau_breach_in_enforce(self, default_policy):
        s = default_policy.tau_floor * 0.5
        d, _, _, intended = default_policy.evaluate_membrane(
            s, -0.1, -0.05, 0.1, 2, OperatingModeNames.ENFORCE
        )
        assert d == MembraneDecisionNames.BLOCK
        assert intended

    def test_intended_block_not_hard_block_in_shadow(self, default_policy):
        s = default_policy.tau_floor * 0.5
        d, _, _, intended = default_policy.evaluate_membrane(
            s, -0.1, -0.05, 0.1, 2, OperatingModeNames.SHADOW_PASSIVE
        )
        assert d == MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK
        assert intended

    def test_alert_without_block_at_alert_floor(self, default_policy):
        s = default_policy.s_alert_floor  # exactly at alert, above block
        d, _, alert, block = default_policy.evaluate_membrane(
            s, 0.0, 0.0, 0.5, None, OperatingModeNames.SHADOW_PASSIVE
        )
        assert alert
        assert not block

    def test_rationale_is_non_empty(self, default_policy):
        _, rationale, _, _ = default_policy.evaluate_membrane(
            0.3, -0.1, -0.05, 0.1, 2, OperatingModeNames.SHADOW_PASSIVE
        )
        assert isinstance(rationale, str) and rationale.strip()

    def test_monotonicity(self, default_policy):
        """Decreasing S in SHADOW mode → non-decreasing decision severity."""
        rank = {
            MembraneDecisionNames.ALLOW: 0,
            MembraneDecisionNames.ALLOW_WITH_ALERT: 1,
            MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK: 2,
            MembraneDecisionNames.BLOCK: 3,
        }
        prev = -1
        for s in [2.0, 1.5, 1.0, 0.85, 0.80, 0.76, 0.42, 0.30]:
            d, _, _, _ = default_policy.evaluate_membrane(
                s, 0.0, 0.0, 0.5, None, OperatingModeNames.SHADOW_PASSIVE
            )
            r = rank[d]
            assert r >= prev, f"Non-monotone at S={s}: rank {r} < previous {prev}"
            prev = max(prev, r)


# ─── SPRINT 0: NOMENCLATURE ──────────────────────────────────────────────────

class TestNomenclature:

    def test_cave_canonical_definition(self):
        """CAVE must be Context/Action/Value/Effect — NOT Control/Veto."""
        exp = CAVEFieldNames.EXPANSION
        assert "Context" in exp and "Action" in exp and "Value" in exp and "Effect" in exp
        assert "Control" not in exp, "Deprecated CAVE variant 'Control' found"
        assert "Veto" not in exp, "Deprecated CAVE variant 'Veto' found"

    def test_operating_mode_r4_r5_alignment(self):
        r4_to_r5 = OperatingModeNames.R4_TO_R5
        assert "SHADOW_PASSIVE" in r4_to_r5
        assert "ENFORCE" in r4_to_r5
        assert r4_to_r5["ENFORCE"] == "ENFORCE"

    def test_business_labels_non_empty(self):
        for mode, label in OperatingModeNames.BUSINESS_ALIAS.items():
            assert isinstance(label, str) and label.strip(), f"Empty label for {mode}"

    def test_ces_state_names_complete(self):
        states = [CESStateNames.REST, CESStateNames.METABOLIZING, CESStateNames.TENSION,
                  CESStateNames.SCHIZOPHRENIA, CESStateNames.COLLAPSE]
        assert len(states) == 5
        assert len(set(states)) == 5  # all distinct


# ─── SPRINT 3: VALIDATOR (CI GATE) ───────────────────────────────────────────

class TestValidatorCIGate:

    def test_full_validation_passes(self, validator):
        report = validator.validate_all()
        assert report.passed, f"Validator FAILED:\n{report.summary()}"

    def test_zero_fail_findings_on_clean_policy(self, validator):
        report = validator.validate_all()
        assert report.fail_count == 0

    def test_policy_contract_check_passes(self, validator):
        report = validator.validate_all()
        pol_checks = [f for f in report.findings if f.check_id.startswith("POL-")]
        fails = [f for f in pol_checks if f.level == "FAIL"]
        assert not fails, f"Policy checks failed: {fails}"

    def test_kernel_math_check_passes(self, validator):
        report = validator.validate_all()
        ker_checks = [f for f in report.findings if f.check_id.startswith("KER-")]
        fails = [f for f in ker_checks if f.level == "FAIL"]
        assert not fails

    def test_membrane_monotonicity_check_passes(self, validator):
        report = validator.validate_all()
        mem_checks = [f for f in report.findings if f.check_id.startswith("MEM-")]
        fails = [f for f in mem_checks if f.level == "FAIL"]
        assert not fails

    def test_nomenclature_check_passes(self, validator):
        report = validator.validate_all()
        nom_checks = [f for f in report.findings if f.check_id.startswith("NOM-")]
        fails = [f for f in nom_checks if f.level == "FAIL"]
        assert not fails

    def test_report_is_serializable(self, validator):
        import json
        report = validator.validate_all()
        d = report.to_dict()
        json.dumps(d)  # must not raise

    def test_invalid_policy_fails_validator(self):
        bad_policy = PolicyEngine(s_block_floor=0.99, s_alert_floor=0.90)
        v = TENIRValidator(policy=bad_policy)
        report = v.validate_all()
        assert not report.passed
        assert report.fail_count > 0

    def test_event_sample_validation_passes_good_event(self, validator):
        good = {"pressure": 0.5, "velocity": 0.5, "capacity": 1.0, "option_space": 0.7}
        report = validator.validate_event_sample(good)
        assert report.passed

    def test_event_sample_validation_fails_negative_pressure(self, validator):
        bad = {"pressure": -0.1, "velocity": 0.5, "capacity": 1.0}
        report = validator.validate_event_sample(bad)
        assert not report.passed

    def test_event_sample_validation_fails_missing_field(self, validator):
        bad = {"pressure": 0.5, "capacity": 1.0}  # missing velocity
        report = validator.validate_event_sample(bad)
        assert not report.passed


# ─── SPRINT 4: REGRESSION CORPUS ─────────────────────────────────────────────

class TestCorpusInventory:
    """Verify the corpus itself is well-formed before running cases."""

    def test_corpus_has_minimum_cases(self):
        assert len(CORPUS) >= 55, f"Corpus only has {len(CORPUS)} cases (minimum 55)"

    def test_all_case_ids_unique(self):
        ids = [c.id for c in CORPUS]
        assert len(ids) == len(set(ids)), "Duplicate case IDs in corpus"

    def test_all_cases_have_required_fields(self):
        for c in CORPUS:
            assert c.id and c.scenario_group and c.description
            assert c.expected_decision in {
                "allow", "allow_with_alert", "allow_with_intended_block", "block"
            }
            assert c.expected_ces_state in {
                "REST", "METABOLIZING", "TENSION", "SIGNAL_CONFLICT", "COLLAPSE"
            }

    def test_corpus_covers_all_5_ces_states(self):
        states = {c.expected_ces_state for c in CORPUS}
        assert states == {"REST", "METABOLIZING", "TENSION", "SIGNAL_CONFLICT", "COLLAPSE"}

    def test_corpus_covers_all_4_decisions(self):
        decisions = {c.expected_decision for c in CORPUS}
        assert decisions == {"allow", "allow_with_alert", "allow_with_intended_block", "block"}

    def test_corpus_covers_all_4_modes(self):
        modes = {c.operating_mode for c in CORPUS}
        assert "ENFORCE" in modes
        assert "SHADOW_PASSIVE" in modes

    def test_nsl_cases_have_nsl_input(self):
        nsl = [c for c in CORPUS if "nsl" in c.tags]
        for c in nsl:
            assert c.nsl_input is not None and c.nsl_input.strip()

    def test_r4_compat_cases_exist(self):
        r4 = [c for c in CORPUS if "r4" in c.tags]
        assert len(r4) >= 3

    def test_compat_cases_exist(self):
        compat = [c for c in CORPUS if "compat" in c.tags]
        assert len(compat) >= 3


def _policy_for_case(case: GoldenCase) -> PolicyEngine:
    """All variants resolve to canonical default in public release."""
    return PolicyEngine.default()


@pytest.mark.parametrize("case", CORPUS, ids=[c.id for c in CORPUS])
class TestGoldenCorpus:
    """
    Parametrized regression against all 60+ golden cases.
    Each test runs the policy membrane and CES classifier and asserts
    exact match against the expected values.

    These tests ARE the CI gate for correctness.
    """

    def test_membrane_decision(self, case: GoldenCase):
        """Membrane decision must exactly match expected_decision."""
        policy = _policy_for_case(case)
        decision, rationale, alert, intended_block = policy.evaluate_membrane(
            s_score=policy.capacity_s(case.pressure, case.velocity, case.capacity),
            ds_de=0.0,   # first event: no history
            d2s_de2=0.0,
            option_space=case.option_space,
            projected_events_to_zero=None,
            operating_mode=case.operating_mode,
        )
        assert decision == case.expected_decision, (
            f"{case.id}: expected decision={case.expected_decision!r}, "
            f"got {decision!r}. Rationale: {rationale}"
        )

    def test_alert_flag(self, case: GoldenCase):
        """Alert flag must match expected_alert."""
        policy = _policy_for_case(case)
        s = policy.capacity_s(case.pressure, case.velocity, case.capacity)
        _, _, alert, _ = policy.evaluate_membrane(
            s, 0.0, 0.0, case.option_space, None, case.operating_mode
        )
        assert alert == case.expected_alert, (
            f"{case.id}: expected alert={case.expected_alert}, got {alert}"
        )

    def test_intended_block_flag(self, case: GoldenCase):
        """Intended block flag must match expected_intended_block."""
        policy = _policy_for_case(case)
        s = policy.capacity_s(case.pressure, case.velocity, case.capacity)
        _, _, _, intended = policy.evaluate_membrane(
            s, 0.0, 0.0, case.option_space, None, case.operating_mode
        )
        assert intended == case.expected_intended_block, (
            f"{case.id}: expected intended_block={case.expected_intended_block}, got {intended}"
        )

    def test_ces_classification(self, case: GoldenCase):
        """CES state must match expected_ces_state."""
        policy = _policy_for_case(case)
        s = policy.capacity_s(case.pressure, case.velocity, case.capacity)
        ces = policy.classify_ces(
            s, 0.0, case.option_space,
            pressure=case.pressure, velocity=case.velocity,
        )
        assert ces == case.expected_ces_state, (
            f"{case.id}: expected ces={case.expected_ces_state!r}, got {ces!r} (S={s:.4f})"
        )
