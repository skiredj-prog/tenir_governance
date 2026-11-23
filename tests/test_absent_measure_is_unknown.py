"""An absent P, V or K is unknown, like an invalid one (candidate 5.7.7, not released).

Normative orientation of 22 November 2026, pending ratification, on the
author's rule of 20 November 2026 (not ratified either):
  R1  K = 0 measured and valid, P or V unknown           -> HARD_VETO
  R2  K unknown, no other conclusive veto                -> HOLD
  R3  K > 0 valid, P or V unknown, no independent veto   -> HOLD
  R4  an independent conclusive veto, whatever K         -> HARD_VETO
5.7.6 applied this to invalid values (NaN, inf, outside [0, 1], non-numbers)
but not to an absent one (None, JSON null): an absent P, V or K was a hard gate,
and an absent K was read as the placeholder 0.0, which named a TAU BREACH on a
capacity nobody measured. Here an absent P, V or K is insufficient evidence:
HOLD, never PASS or FLAG, and no veto is justified by a placeholder. A
conclusive veto that does not depend on the absent measure still governs.

The author extended this on 23 November 2026 (rule A, pending ratification of
the amended RFC-2 v0.2) to every mandatory measure, authority, context_fit and
evidence_quality included: see test_ep01_epistemic_boundary.py and
test_absent_measure_hold_lifecycle.py.
"""
import json

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Passage, Resource
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_kernel.kernel import TenirKernel

BEST = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.2, capacity=0.9, authority=0.9,
            context_fit=1.0, evidence_quality=1.0, option_space=0.9)
PVC = ("pressure", "velocity", "capacity")
CONDITIONS = ("review",)


def _adapter():
    return ScoringKernelAdapter(TenirKernel())


def _governor():
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    return DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                              policy=CompositionPolicy(flag_conditions=CONDITIONS))


@pytest.mark.parametrize("absent", [("capacity",), ("pressure",), ("velocity",),
                                    ("pressure", "capacity"), PVC])
def test_absent_p_v_or_k_holds_without_an_s(absent):
    r = _adapter().adjudicate(**{**BEST, **{a: None for a in absent}})
    assert r["decision"] == Decision.HOLD.value
    assert r["s_score"] is None
    named = ", ".join(n for n in PVC if n in absent)
    assert f"INSUFFICIENT_EVIDENCE: absent measure(s) {named}; S not computed." in r["rationale"]
    assert "TAU BREACH" not in r["rationale"]
    assert set(absent) <= set(r["declared_unmeasured"])


def test_an_absent_capacity_is_never_read_as_zero():
    """5.7.6 named 'TAU BREACH: S=0.0000' on the placeholder capacity 0.0."""
    r = _adapter().adjudicate(**{**BEST, "capacity": None})
    assert r["decision"] == Decision.HOLD.value
    assert "TAU BREACH" not in r["rationale"]
    assert "cannot score S" not in r["rationale"]


def test_absent_and_invalid_are_both_named():
    r = _adapter().adjudicate(**{**BEST, "irreversibility_materiality": 0.0, "pressure": float("nan"), "capacity": None})
    assert r["decision"] == Decision.HOLD.value
    assert ("INSUFFICIENT_EVIDENCE: invalid measure(s) (non-finite or outside [0, 1]) pressure; "
            "absent measure(s) capacity; S not computed.") in r["rationale"]
    assert r["unmeasured_axes"] == ["pressure"]


@pytest.mark.parametrize("absent", ["pressure", "velocity"])
def test_r1_measured_zero_capacity_vetoes_whatever_the_absent_measure(absent):
    """S = C / (P·V + ε) is 0 whatever P and V are when C is measured at 0."""
    r = _adapter().adjudicate(**{**BEST, "capacity": 0.0, absent: None})
    assert r["decision"] == Decision.HARD_VETO.value
    assert "TAU BREACH: capacity=0.0 gives S=0" in r["rationale"]


@pytest.mark.parametrize("veto, named", [
    (dict(authority=0.0), None),
    (dict(context_fit=0.0), None),
    (dict(option_space=0.0), "option_space=0.000 ≤ block floor"),
])
def test_r4_an_independent_veto_governs_an_absent_capacity(veto, named):
    r = _adapter().adjudicate(**{**BEST, "capacity": None, **veto})
    assert r["decision"] == Decision.HARD_VETO.value
    assert "TAU BREACH" not in r["rationale"]
    if named:
        assert named in r["rationale"]


def test_governor_holds_an_absent_capacity_and_the_hold_stays_recoverable():
    gov = _governor()
    p = Passage(agent_id="a", action_type="x", idempotency_key="k-absent-capacity")
    r = gov.adjudicate(p, payload={"action_type": "x"}, **{**BEST, "capacity": None})
    assert r["scoring_decision"] == Decision.HOLD.value
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["operation_state"] == "hold"
    assert "TAU BREACH" not in (r["composed_rationale"] + r["scoring_rationale"])
    # The measure arrives: the same attempt can be re-adjudicated and go forward.
    rr = gov.resolve_hold("k-absent-capacity", passage=p, payload={"action_type": "x"}, **BEST)
    assert rr["hold_resolved"] is True
    assert rr["composed_decision"] in (Decision.PASS.value, Decision.FLAG.value)


def test_r4_a_membrane_invariant_vetoes_whatever_the_absent_capacity():
    gov = _governor()
    p = Passage(agent_id="a", action_type="x", amount=500.0, idempotency_key="k-absent-i8")
    r = gov.adjudicate(p, payload={"action_type": "x"}, **{**BEST, "capacity": None},
                       resource_id="budget", amount=500.0)
    assert r["composed_decision"] == Decision.HARD_VETO.value


def test_json_null_capacity_on_the_sdk_path_holds():
    """Decision 6: null on the wire is an absent measure, not a zero."""
    client = DualKernelCompositionClient(policy=CompositionPolicy(flag_conditions=CONDITIONS, hold_tau_k_seconds=3600.0, hold_tau_k_source="test fixture: one hour, not an SLA"))
    client.register_resource(Resource(resource_id="budget", capacity=100.0))
    measured = json.loads(json.dumps({**BEST, "capacity": None}))
    r = client.adjudicate(Passage(agent_id="a", action_type="x"), **measured)
    assert r["composed_decision"] == Decision.HOLD.value
    assert "TAU BREACH" not in str(r.get("composed_rationale")) + str(r.get("scoring_rationale"))
