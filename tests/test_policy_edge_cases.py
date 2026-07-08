import pytest
from tenir_governance.policy_engine import PolicyEngine, PolicyViolation

def test_policy_validation_version_failure():
    # Trigger _check_version branch
    p = PolicyEngine(version="") 
    with pytest.raises(PolicyViolation, match="must be a non-empty string"):
        p.validate()

def test_policy_validation_float_failure():
    # Trigger _check_floats minimum limit branch
    p = PolicyEngine(tau_floor=-0.1)
    with pytest.raises(PolicyViolation, match="must be >="):
        p.validate()

def test_policy_validation_ordering_failure():
    # Trigger _check_ordering branch (tau > block)
    p = PolicyEngine(tau_floor=0.80, s_block_floor=0.75)
    with pytest.raises(PolicyViolation, match="tau_floor must be < s_block_floor"):
        p.validate()

def test_policy_version_drift_guard():
    p = PolicyEngine.default()
    with pytest.raises(PolicyViolation, match="Policy version mismatch"):
        p.assert_version_matches("tenir-wrong-version-1.0.0")

def test_r4_policy_bundle_export():
    p = PolicyEngine.default()
    bundle = p.to_r4_policy_bundle()
    assert bundle["version"] == p.version
    assert bundle["s_alert_floor"] == p.s_alert_floor

def test_ces_metabolizing_branch():
    p = PolicyEngine.default()
    # Trigger the METABOLIZING doctrine branch: S > alert, activity >= 1.00
    state = p.classify_ces(s_score=0.95, ds_de=0.0, option_space=1.0, pressure=1.5, velocity=1.0)
    assert state == "METABOLIZING"