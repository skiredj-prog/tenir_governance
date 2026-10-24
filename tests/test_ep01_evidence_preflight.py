"""EP-01 signature, standing and temporal preflight: isolated prototype tests."""
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep01_policy import Claim, Verdict
from tenir_governance.experimental.ep01_evidence import (
    SignedClaim, canonical_claim_bytes, verify_and_evaluate,
)


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
PRIV = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def case(claim=None, *, key=PRIV, scopes=None, now=NOW):
    claim = claim or Claim("B", "council", "A", NOW)
    proof = SignedClaim(claim, key.sign(canonical_claim_bytes(claim)))
    return verify_and_evaluate(
        (proof,), prior="A", scope="contracts",
        keys={"council": PRIV.public_key()},
        mandates={"council": frozenset({"contracts"}) if scopes is None else scopes},
        now=now,
    )


def test_accepted_preflight_does_not_claim_runtime_commit():
    assert case() == (Verdict.ADMISSIBLE, "POLICY_PRECONDITIONS_MET")


def test_signature_from_wrong_private_key_rejected():
    other = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    assert case(key=other) == (Verdict.REJECT, "INVALID_SIGNATURE")


def test_valid_signature_without_issuer_standing_rejected():
    assert case(scopes=frozenset()) == (Verdict.REJECT, "INSUFFICIENT_STANDING")


def test_signed_future_document_stays_on_hold():
    assert case(Claim("B", "council", "A", NOW + timedelta(days=1))) == (
        Verdict.HOLD, "NOT_YET_EFFECTIVE")


def test_tampering_after_signing_cannot_change_supersession():
    a = Claim("B", "council", "A", NOW)
    tampered = Claim("B", "council", "X", NOW)
    proof = SignedClaim(tampered, PRIV.sign(canonical_claim_bytes(a)))
    result = verify_and_evaluate(
        (proof,), prior="A", scope="contracts",
        keys={"council": PRIV.public_key()},
        mandates={"council": frozenset({"contracts"})}, now=NOW)
    assert result == (Verdict.REJECT, "INVALID_SIGNATURE")
