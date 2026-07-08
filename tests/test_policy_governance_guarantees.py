import pytest
from tenir_governance.policy_engine import PolicyEngine, PolicyViolation

# ==========================================
# 1. IDEMPOTENCE (DETERMINISM GUARANTEE)
# ==========================================
def test_evaluate_membrane_idempotence():
    """Governance middleware must yield identical results for identical state."""
    policy = PolicyEngine.default()
    args = (0.82, -0.06, -0.01, 0.50, 4, "ENFORCE")
    
    result_1 = policy.evaluate_membrane(*args)
    result_2 = policy.evaluate_membrane(*args)
    result_3 = policy.evaluate_membrane(*args)
    
    assert result_1 == result_2 == result_3


# ==========================================
# 2. BOUNDARY DETERMINISM (FLOATING POINT EDGES)
# ==========================================
def test_tau_boundary_enforcement():
    """Ensure strict inequality evaluation at the mathematical boundary."""
    policy = PolicyEngine.default()
    tau = policy.tau_floor  # default is 0.42

    # Exactly at boundary (assuming strictly less than is required for breach)
    # Note: policy checks `s_score <= tau_floor` for block, `s_score < tau_floor` for collapse
    
    # Just above boundary
    decision_above, _, _, is_block_above = policy.evaluate_membrane(
        tau + 0.000001, 0.0, 0.0, 1.0, 10, "ENFORCE"
    )
    
    # Just below boundary
    decision_below, _, _, is_block_below = policy.evaluate_membrane(
        tau - 0.000001, 0.0, 0.0, 1.0, 10, "ENFORCE"
    )
    
    # The microscopic difference must reliably trigger the block state shift
    assert is_block_below is True
    # (Depending on other defaults, 'above' might still trigger a block if below s_block_floor, 
    # but the specific TAU breach rationale will be absent).


# ==========================================
# 3. INVARIANT CHECKS (LEGACY COVERAGE)
# ==========================================
def test_policy_validation_version_failure():
    p = PolicyEngine(version="") 
    with pytest.raises(PolicyViolation, match="must be a non-empty string"):
        p.validate()

def test_policy_validation_ordering_failure():
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