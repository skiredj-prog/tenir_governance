"""EP-01 characterization: epistemic sufficiency != document authority.

These tests check existing kernel boundaries only. They intentionally do not
claim that TENIR authenticates or adjudicates contradictory documents.
"""
import pytest

from tenir_governance.composition import ScoringKernelAdapter
from tenir_kernel.kernel import TenirKernel


def score(**overrides):
    values = dict(pressure=0.2, velocity=0.2, capacity=0.9,
                  authority=0.9, context_fit=0.9, evidence_quality=0.9)
    values.update(overrides)
    return ScoringKernelAdapter(TenirKernel()).adjudicate(**values)


@pytest.mark.parametrize("missing", ["authority", "context_fit", "evidence_quality"])
def test_ep01_missing_epistemic_input_hard_veto(missing):
    """Current implementation hard-gates *missing measurements*."""
    result = score(**{missing: None})
    assert result["decision"] == "HARD_VETO", (missing, result)


def test_ep01_low_authority_is_not_missing_authority():
    """Zero is measured input; cannot equate lack of standing to absent value."""
    result = score(authority=0.0)
    assert result["authority"] == 0.0
    assert result["constraint_geometry"] is not None


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
