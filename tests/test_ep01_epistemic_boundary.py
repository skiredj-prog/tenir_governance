"""EP-01 characterization: epistemic sufficiency != document authority.

These tests check existing kernel boundaries only. They intentionally do not
claim that TENIR authenticates or adjudicates contradictory documents.

Rule A (candidate 5.7.7, not released; author's decision of 23 November 2026,
pending ratification of the amended RFC-2 v0.2): a missing epistemic input is
unknown, exactly like an invalid one. The absence of a measure is proof neither
of a violation nor of admissibility: HOLD, unless a conclusive veto governs,
one the valid measures establish whatever value the unknown ones would take.
No veto rests on a placeholder. 5.7.6 hard-gated a missing authority,
context_fit or evidence_quality (HARD_VETO, terminal for the attempt), and
vetoed an invalid authority on its placeholder 0.5.
"""
import math

import pytest

from tenir_governance.composition import ScoringKernelAdapter
from tenir_kernel.geometry import REFERENCE_GEOMETRY, Facet, GeometryPolicy
from tenir_kernel.kernel import KernelPolicy, TenirKernel

EPISTEMIC = ("authority", "context_fit", "evidence_quality")


def score(kernel=None, **overrides):
    values = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.2, capacity=0.9, option_space=0.9,
                  authority=0.9, context_fit=0.9, evidence_quality=0.9)
    values.update(overrides)
    return ScoringKernelAdapter(kernel or TenirKernel()).adjudicate(**values)


@pytest.mark.parametrize("missing", EPISTEMIC)
def test_ep01_missing_epistemic_input_holds(missing):
    """A missing measurement is insufficient evidence: HOLD, not a hard gate."""
    result = score(**{missing: None})
    assert result["decision"] == "HOLD", (missing, result)
    assert f"INSUFFICIENT_EVIDENCE: absent measure(s) {missing};" in result["rationale"]
    assert "missing: cannot adjudicate" not in result["rationale"]
    assert missing in result["declared_unmeasured"]
    assert result[missing] is None
    assert result["s_score"] is not None  # S is computed from the measured P, V and C
    assert result["constraint_geometry"] is None  # no facet evaluated on a placeholder


@pytest.mark.parametrize("missing", EPISTEMIC)
def test_ep01_missing_and_invalid_epistemic_input_hold_alike(missing):
    absent = score(**{missing: None})
    invalid = score(**{missing: math.nan})
    assert absent["decision"] == invalid["decision"] == "HOLD"
    assert absent["s_score"] == invalid["s_score"]
    assert invalid["unmeasured_axes"] == [missing] and absent["unmeasured_axes"] == []


@pytest.mark.parametrize("missing", EPISTEMIC)
@pytest.mark.parametrize("veto, reason", [
    (dict(irreversibility_materiality=0.0, pressure=0.95, velocity=0.9, capacity=0.1), "TAU BREACH: S="),
    (dict(irreversibility_materiality=0.0, pressure=0.9, velocity=0.9, capacity=0.5), "is at or below hard-veto floor 0.75"),
    (dict(capacity=0.0), "TAU BREACH: S="),
    (dict(option_space=0.1), "option_space=0.100 ≤ block floor"),
])
def test_ep01_conclusive_veto_governs_a_missing_epistemic_input(missing, veto, reason):
    """A veto the valid measures establish keeps its priority (RFC-0 §4)."""
    result = score(**{missing: None, **veto})
    assert result["decision"] == "HARD_VETO", result
    assert reason in result["rationale"]
    assert f"Conclusive whatever the unknown measure(s) are: absent measure(s) {missing}." \
        in result["rationale"]


@pytest.mark.parametrize("missing, floor, named", [
    ("authority", dict(context_fit=0.0), "does not sufficiently fit its declared context"),
    ("context_fit", dict(authority=0.0), "lacks the reference minimum authority score"),
    ("evidence_quality", dict(authority=0.0), "lacks the reference minimum authority score"),
    ("evidence_quality", dict(context_fit=0.0), "does not sufficiently fit its declared context"),
])
def test_ep01_a_measured_floor_veto_governs_another_missing_input(missing, floor, named):
    result = score(**{missing: None, **floor})
    assert result["decision"] == "HARD_VETO"
    assert named in result["rationale"]


def test_ep01_low_authority_is_not_missing_authority():
    """Zero is measured input; cannot equate lack of standing to absent value.
    A measured authority of zero meets the reference authority floor: veto."""
    result = score(authority=0.0)
    assert result["authority"] == 0.0
    assert result["constraint_geometry"] is not None
    assert result["decision"] == "HARD_VETO"
    assert "lacks the reference minimum authority score" in result["rationale"]


@pytest.mark.parametrize("unknown", [None, math.nan])
def test_ep01_no_veto_on_the_authority_placeholder(unknown):
    """5.7.6 vetoed an invalid authority with irreversibility 0.8 on its
    placeholder 0.5 ('Irreversible-pathway materiality exceeds the authority
    margin'). A measured authority of 1.0 satisfies that facet, so the veto
    was not conclusive: it rested on a value nobody measured."""
    result = score(authority=unknown, irreversibility_materiality=0.8)
    assert result["decision"] == "HOLD"
    assert "exceeds the authority margin" not in result["rationale"]
    # Once measured, the facet is evaluated on the measure.
    measured_low = score(authority=0.5, irreversibility_materiality=0.8)
    assert measured_low["decision"] == "HARD_VETO"
    assert "exceeds the authority margin" in measured_low["rationale"]
    assert score(authority=1.0, irreversibility_materiality=0.8)["decision"] in ("PASS", "FLAG")


def test_ep01_explicitly_absent_irreversibility_holds_instead_of_raising():
    """5.7.6 raised on irreversibility_materiality=None, and the governor's
    fail-closed HOLD then masked any conclusive veto, such as a TAU BREACH."""
    assert score(irreversibility_materiality=None)["decision"] == "HOLD"
    vetoed = score(irreversibility_materiality=None, capacity=0.0)
    assert vetoed["decision"] == "HARD_VETO"
    assert "TAU BREACH" in vetoed["rationale"]


def _kernel_with(facet):
    geometry = GeometryPolicy(REFERENCE_GEOMETRY.geometry_id, "test-geometry",
                              REFERENCE_GEOMETRY.facets + (facet,))
    return TenirKernel(KernelPolicy(geometry=geometry))


def test_ep01_a_facet_violated_whatever_its_unknown_axis_is_conclusive():
    """A hard facet is conclusive when even the most favorable value of its
    unknown axes in [0, 1] violates it, not only when every axis is measured."""
    kernel = _kernel_with(Facet(
        "test-irreversibility-margin",
        {"irreversible_pathway_materiality": 1.0, "authority": -0.5}, 0.1, "hard",
        "Test facet: irreversibility beyond any authority margin.",
    ))
    vetoed = score(kernel, authority=None, irreversibility_materiality=0.9)  # 0.9 - 0.5a >= 0.4
    assert vetoed["decision"] == "HARD_VETO"
    assert "beyond any authority margin" in vetoed["rationale"]
    held = score(kernel, authority=None, irreversibility_materiality=0.5)  # 0.5 - 0.5a can be 0
    assert held["decision"] == "HOLD"


def test_ep01_two_authentic_claims_not_an_authority_resolution():
    """Signature authenticity is insufficient to establish legal precedence."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pk = key.public_key()
    first = b'{"document":"A","effective":"current"}'
    second = b'{"document":"B","supersedes":"A"}'
    for payload in (first, second):
        pk.verify(key.sign(payload), payload)
    # Both pass authentication. No claim is made that scoring can tell which
    # document controls; it only sees numeric authority/evidence/context axes.
    one = score()
    two = score()
    assert one["decision"] == two["decision"]


def test_ep01_temporal_precedence_not_a_scoring_input():
    """A future effective date is not represented by the existing score API."""
    import inspect
    params = inspect.signature(ScoringKernelAdapter.adjudicate).parameters
    assert "effective_at" not in params
    assert "supersedes" not in params
    assert "issuer_scope" not in params


# --- Decision of 23 November 2026, 04:54 UTC: an omitted irreversibility is unknown ---

def _without(**overrides):
    """Adjudicate with every measure but irreversibility_materiality, which is
    omitted, not passed: the caller never measured it."""
    values = dict(pressure=0.2, velocity=0.2, capacity=0.9, option_space=0.9,
                  authority=0.9, context_fit=0.9, evidence_quality=0.9)
    values.update(overrides)
    return ScoringKernelAdapter(TenirKernel()).adjudicate(**values)


def test_omitted_irreversibility_is_unknown_and_holds():
    """5.7.6 defaulted the omitted axis to 0.0, the most favourable value, so a
    caller that never measured it was adjudicated as if it had measured none.
    An omission is now indistinguishable from an explicit None: HOLD."""
    result = _without()
    assert result["decision"] == "HOLD", result
    assert "absent measure(s) irreversibility_materiality" in result["rationale"]
    assert "irreversibility_materiality" in result["declared_unmeasured"]
    assert result["constraint_geometry"] is None
    assert result["insufficient_evidence"] is True


def test_omitted_irreversibility_differs_from_a_measured_zero():
    """0.0 passed explicitly is a measure and keeps the 5.7.6 result."""
    measured = _without(irreversibility_materiality=0.0)
    assert measured["decision"] in ("PASS", "FLAG"), measured
    assert "irreversibility_materiality" not in measured["declared_unmeasured"]


def test_conclusive_veto_governs_an_omitted_irreversibility():
    vetoed = _without(capacity=0.0)
    assert vetoed["decision"] == "HARD_VETO"
    assert "TAU BREACH" in vetoed["rationale"]
    floor = _without(authority=0.0)
    assert floor["decision"] == "HARD_VETO", floor


def test_omitted_irreversibility_never_vetoes_on_the_materiality_facet():
    """The facet 'irreversible-pathway materiality exceeds the authority
    margin' is evaluated on a measure or not at all: an unknown materiality
    gives no veto on it, whatever the measured authority is."""
    for authority in (0.5, 0.9):
        r = _without(authority=authority)
        assert r["decision"] == "HOLD", (authority, r)
        assert "exceeds the authority margin" not in r["rationale"]
    # A measured authority at the floor is a conclusive veto of its own, and
    # it governs the unknown materiality, as rule A says.
    assert _without(authority=0.3)["decision"] == "HARD_VETO"


def test_governor_holds_an_omitted_irreversibility_and_reserves_nothing():
    from tenir_governance.composition import DualKernelGovernor
    from tenir_governance.membrane import MembraneState, TransitionMembrane
    from tenir_governance.objects import Agent, Passage, Resource
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state))
    r = gov.adjudicate(Passage(agent_id="alice", action_type="payment", amount=10.0),
                       pressure=0.2, velocity=0.3, capacity=1.0, option_space=0.9,
                       authority=0.9, context_fit=0.9, evidence_quality=0.9,
                       resource_id="budget", amount=10.0)
    assert r["composed_decision"] == "HOLD"
    assert "irreversibility_materiality" in r["declared_unmeasured"]
    assert r["budget_reserved"] is False
    assert state.resources["budget"].reserved == 0.0


def test_sdk_adjudicate_has_no_default_for_irreversibility():
    import inspect
    from tenir_governance.sdk import DualKernelCompositionClient
    params = inspect.signature(DualKernelCompositionClient.adjudicate).parameters
    assert params["irreversibility_materiality"].default is None
