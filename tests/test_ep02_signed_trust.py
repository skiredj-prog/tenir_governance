"""EP-02 signed-trust tests: authentic root, revocation, drift, claim binding."""
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep01_policy import Claim, Verdict
from tenir_governance.experimental.ep01_evidence import SignedClaim, canonical_claim_bytes
from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState, canonical_trust_bytes, verify_trust_snapshot,
    verified_preflight, verified_revalidation,
)

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def fixture():
    root = Ed25519PrivateKey.generate()
    issuer = Ed25519PrivateKey.generate()
    state = TrustState(1, "policy-v1", (("issuer", "contracts"),))
    signed = SignedTrustState(state, root.sign(canonical_trust_bytes(state)), "root")
    claim = Claim("B", "issuer", "A", NOW)
    proof = SignedClaim(claim, issuer.sign(canonical_claim_bytes(claim)))
    roots = {"root": root.public_key()}
    issuers = {"issuer": issuer.public_key()}
    return root, signed, proof, roots, issuers


def preflight(signed, proof, roots, issuers, minimum=1):
    return verified_preflight(proof, prior="A", scope="contracts", now=NOW,
                              trust=signed, root_keys=roots,
                              issuer_keys=issuers, minimum_revision=minimum)


def test_valid_signed_trust_still_only_preflight():
    _, signed, proof, roots, issuers = fixture()
    result, message, receipt = preflight(signed, proof, roots, issuers)
    assert (result, message) == (Verdict.ADMISSIBLE, "VERIFIED_PREFLIGHT_ONLY")
    assert receipt is not None
    assert verified_revalidation(
        receipt, proof, prior="A", scope="contracts", now=NOW,
        current=signed, root_keys=roots, issuer_keys=issuers,
        minimum_revision=1) == (
            Verdict.ADMISSIBLE, "PREFLIGHT_STILL_VALID_NOT_COMMITTED")


def test_unknown_root_or_tampered_snapshot_rejected():
    root, signed, proof, roots, issuers = fixture()
    assert preflight(signed, proof, {}, issuers)[0] == Verdict.REJECT
    modified = SignedTrustState(
        TrustState(1, "policy-v1", (("issuer", "payments"),)),
        signed.signature, "root")
    assert not verify_trust_snapshot(modified, root_keys=roots)
    assert preflight(modified, proof, roots, issuers)[0] == Verdict.REJECT


def test_revocation_and_mandate_withdrawal_after_preflight():
    root, old, proof, roots, issuers = fixture()
    _, _, receipt = preflight(old, proof, roots, issuers)
    for state in (
        TrustState(2, "policy-v1", old.state.mandates, ("issuer",)),
        TrustState(2, "policy-v1", ()),
    ):
        current = SignedTrustState(
            state, root.sign(canonical_trust_bytes(state)), "root")
        verdict, _ = verified_revalidation(
            receipt, proof, prior="A", scope="contracts", now=NOW,
            current=current, root_keys=roots, issuer_keys=issuers,
            minimum_revision=2)
        assert verdict != Verdict.ADMISSIBLE


def test_old_valid_snapshot_rejected_against_rollback_floor():
    _, signed, proof, roots, issuers = fixture()
    assert preflight(signed, proof, roots, issuers, minimum=2) == (
        Verdict.REJECT, "TRUST_SNAPSHOT_UNVERIFIED", None)


def test_claim_change_after_preflight_not_reused():
    root, signed, proof, roots, issuers = fixture()
    _, _, receipt = preflight(signed, proof, roots, issuers)
    alternate = Claim("C", "issuer", "A", NOW)
    other_priv = Ed25519PrivateKey.generate()
    # Unauthorized new signing key is a signature rejection, not safe reuse.
    changed = SignedClaim(alternate, other_priv.sign(canonical_claim_bytes(alternate)))
    verdict, _ = verified_revalidation(
        receipt, changed, prior="A", scope="contracts", now=NOW,
        current=signed, root_keys=roots, issuer_keys=issuers,
        minimum_revision=1)
    assert verdict == Verdict.REJECT
