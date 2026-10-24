"""EP-02 trust drift / binding characterization tests."""
from tenir_governance.experimental.ep02_binding import TrustState, bind, revalidate


BASE = TrustState(1, "policy-v1", (("council", "contracts"),))


def receipt():
    return bind(trust=BASE, issuer="council", scope="contracts",
                prior_document="A", claim_digest="sha256:proof")


def test_unchanged_trust_binding():
    assert revalidate(receipt(), current=BASE,
                      claim_digest="sha256:proof",
                      prior_document="A", scope="contracts")


def test_revoked_issuer_changes_trust_fingerprint():
    changed = TrustState(2, "policy-v1", BASE.mandates, ("council",))
    assert not revalidate(receipt(), current=changed,
                          claim_digest="sha256:proof",
                          prior_document="A", scope="contracts")
    assert bind(trust=changed, issuer="council", scope="contracts",
                prior_document="A", claim_digest="sha256:proof") is None


def test_mandate_withdrawal_blocks_revalidation():
    changed = TrustState(2, "policy-v1", ())
    assert not revalidate(receipt(), current=changed,
                          claim_digest="sha256:proof",
                          prior_document="A", scope="contracts")


def test_revision_only_change_blocks_revalidation():
    changed = TrustState(2, "policy-v1", BASE.mandates)
    assert not revalidate(receipt(), current=changed,
                          claim_digest="sha256:proof",
                          prior_document="A", scope="contracts")


def test_changed_document_or_claim_blocks_revalidation():
    for digest, prior in (("sha256:other", "A"), ("sha256:proof", "B")):
        assert not revalidate(receipt(), current=BASE,
                              claim_digest=digest, prior_document=prior,
                              scope="contracts")


def test_invalid_or_missing_mandate_fails_closed():
    assert bind(trust=BASE, issuer="outsider", scope="contracts",
                prior_document="A", claim_digest="sha256:proof") is None
