import pytest
from tenir_governance.policy_engine import PolicyEngine

# ==========================================
# 1. COLD INITIALIZATION
# ==========================================
def test_benchmark_initialization(benchmark):
    """Measures cold start: instantiation and invariant validation."""
    def init_and_validate():
        p = PolicyEngine.default()
        p.validate()
        return p
    
    # benchmark() automatically calculates mean, median, min, max, and variance
    benchmark(init_and_validate)


# ==========================================
# 2. DECISION LATENCY (SCENARIO ISOLATION)
# ==========================================
@pytest.fixture(scope="module")
def initialized_policy():
    """Provides a pre-validated policy to isolate decision latency."""
    p = PolicyEngine.default()
    p.validate()
    return p

def test_benchmark_evaluate_nominal_safe(benchmark, initialized_policy):
    """Nominal condition: High S-score, stable horizon, no alerts."""
    benchmark(
        initialized_policy.evaluate_membrane,
        s_score=0.95,
        ds_de=0.0,
        d2s_de2=0.0,
        option_space=0.80,
        projected_events_to_zero=15,
        operating_mode="ENFORCE"
    )

def test_benchmark_evaluate_alert_tension(benchmark, initialized_policy):
    """Alert condition: S-score dipping below alert floor, restricted options."""
    benchmark(
        initialized_policy.evaluate_membrane,
        s_score=0.85,          # Below s_alert_floor (0.90)
        ds_de=-0.06,           # Below ds_de_alert_floor (-0.05)
        d2s_de2=0.0,
        option_space=0.30,     # Below option_space_alert_floor (0.35)
        projected_events_to_zero=4, # Below reaction_budget (5)
        operating_mode="ENFORCE"
    )

def test_benchmark_evaluate_block_signal_conflict(benchmark, initialized_policy):
    """Block condition: S-score breaches TAU floor."""
    benchmark(
        initialized_policy.evaluate_membrane,
        s_score=0.40,          # Below tau_floor (0.42)
        ds_de=-0.10,
        d2s_de2=-0.05,
        option_space=0.15,     # Below option_space_block_floor (0.20)
        projected_events_to_zero=1,
        operating_mode="ENFORCE"
    )