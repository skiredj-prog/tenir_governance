"""No deployment without a declared tau_K (R11, option C, runtime author's
decision of 2026-11-23). The SDK client is the deployment surface of this
runtime: it refuses to construct with a policy that declares no tau_K, and
says why in words an operator can act on. The library-level governor keeps
accepting a policy without tau_K (tests, instruments and the REG harness build
it directly); the library documents the exposure instead."""
import warnings

import pytest

from tenir_governance.composition import CompositionPolicy, DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_kernel.kernel import TenirKernel


def _client(**policy_kw):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return DualKernelCompositionClient(policy=CompositionPolicy(**policy_kw))


def test_client_refuses_a_policy_without_tau_k():
    with pytest.raises(ValueError) as exc:
        _client(flag_conditions=("supervision",))
    message = str(exc.value)
    assert "No deployment without a declared tau_K" in message
    assert "hold_tau_k_seconds" in message and "hold_tau_k_source" in message
    assert "do not invent it" in message


def test_client_refuses_the_default_policy():
    with pytest.raises(ValueError, match="No deployment without a declared tau_K"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            DualKernelCompositionClient()


def test_client_constructs_with_a_sourced_tau_k_and_records_it_on_holds():
    client = _client(hold_tau_k_seconds=4 * 3600.0,
                     hold_tau_k_source="operator SLA: a HOLD is reviewed within 4 h (fixture)")
    assert client._policy.hold_tau_k_seconds == 4 * 3600.0
    assert client._policy.hold_tau_k_source.startswith("operator SLA")


def test_a_tau_k_without_a_source_is_still_refused_at_validation():
    with pytest.raises(ValueError, match="hold_tau_k_source is required"):
        _client(hold_tau_k_seconds=3600.0)


def test_library_governor_still_accepts_a_policy_without_tau_k():
    """Declared limit, not a deployment: the governor invents no bound and holds
    without one. The exposure is documented (CHANGELOG, guide); the SDK refuses it."""
    state = MembraneState()
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=CompositionPolicy())
    assert gov.policy.hold_tau_k_seconds is None
