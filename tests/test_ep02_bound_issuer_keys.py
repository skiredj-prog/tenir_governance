"""EP-02 v2 issuer key binding adversarial tests."""
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep01_policy import Claim, Verdict
from tenir_governance.experimental.ep01_evidence import SignedClaim, canonical_claim_bytes
from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState, bound_snapshot_bytes, verified_bound_preflight,
    revalidate_bound,
)

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def rawkey(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)


def environment():
    root, issuer = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    state = TrustState(1, "policy-v1", (("issuer", "contracts"),))
    skeleton = SignedTrustState(state, b"", "root", (("issuer", rawkey(issuer)),))
    signed = SignedTrustState(
        state, root.sign(bound_snapshot_bytes(skeleton)), "root",
        skeleton.issuer_keys)
    claim = Claim("B", "issuer", "A", NOW)
    proof = SignedClaim(claim, issuer.sign(canonical_claim_bytes(claim)))
    return root, issuer, signed, proof


def preflight(trust, proof, root):
    return verified_bound_preflight(
        proof, prior="A", scope="contracts", now=NOW, trust=trust,
        root_keys={"root": root.public_key()})


def test_root_signed_key_binding_passes_without_external_issuer_keys():
    root, issuer, signed, proof = environment()
    verdict, _, receipt = preflight(signed, proof, root)
    assert verdict == Verdict.ADMISSIBLE
    assert receipt.signed_snapshot_digest
    assert revalidate_bound(
        receipt, proof, prior="A", scope="contracts", now=NOW,
        current=signed, root_keys={"root": root.public_key()},
        minimum_revision=1)[0] == Verdict.ADMISSIBLE


def test_substituted_issuer_key_without_root_resigning_rejected():
    root, issuer, signed, proof = environment()
    attacker = Ed25519PrivateKey.generate()
    substitute = SignedTrustState(
        signed.state, signed.signature, "root",
        (("issuer", rawkey(attacker)),))
    assert preflight(substitute, proof, root)[0] == Verdict.REJECT


def test_root_signed_new_key_same_revision_invalidates_old_receipt():
    root, issuer, signed, proof = environment()
    _, _, receipt = preflight(signed, proof, root)
    other = Ed25519PrivateKey.generate()
    skeleton = SignedTrustState(
        signed.state, b"", "root", (("issuer", rawkey(other)),))
    changed = SignedTrustState(
        signed.state, root.sign(bound_snapshot_bytes(skeleton)),
        "root", skeleton.issuer_keys)
    result = revalidate_bound(
        receipt, proof, prior="A", scope="contracts", now=NOW,
        current=changed, root_keys={"root": root.public_key()},
        minimum_revision=1)
    assert result[0] != Verdict.ADMISSIBLE


def test_missing_key_binding_rejected():
    root, issuer, signed, proof = environment()
    skeleton = SignedTrustState(signed.state, b"", "root")
    unbound = SignedTrustState(
        signed.state, root.sign(bound_snapshot_bytes(skeleton)), "root")
    verdict, reason, _ = preflight(unbound, proof, root)
    assert verdict == Verdict.REJECT
    assert reason == "ISSUER_KEY_NOT_BOUND"


def test_revoked_issuer_rejected_even_with_valid_signatures():
    root, issuer, signed, proof = environment()
    revoked = TrustState(2, "policy-v1", signed.state.mandates, ("issuer",))
    skeleton = SignedTrustState(
        revoked, b"", "root", signed.issuer_keys)
    current = SignedTrustState(
        revoked, root.sign(bound_snapshot_bytes(skeleton)), "root",
        signed.issuer_keys)
    assert preflight(current, proof, root)[0] == Verdict.REJECT
