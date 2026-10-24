"""EP-02 pure trust binding prototype; not wired to runtime."""
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class TrustState:
    revision: int
    policy_version: str
    mandates: tuple[tuple[str, str], ...]
    revoked: tuple[str, ...] = ()

    def digest(self) -> str:
        data = (self.revision, self.policy_version,
                sorted(self.mandates), sorted(self.revoked))
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()


@dataclass(frozen=True)
class BoundFinding:
    trust_revision: int
    trust_digest: str
    policy_version: str
    claim_digest: str
    prior_document: str
    scope: str
    signed_snapshot_digest: str = ""


def bind(*, trust: TrustState, issuer: str, scope: str,
         prior_document: str, claim_digest: str) -> BoundFinding | None:
    if (trust.revision < 1 or not trust.policy_version or not claim_digest
        or not prior_document or issuer in trust.revoked
        or (issuer, scope) not in trust.mandates):
        return None
    return BoundFinding(trust.revision, trust.digest(), trust.policy_version,
                        claim_digest, prior_document, scope)


def revalidate(binding: BoundFinding, *, current: TrustState,
               claim_digest: str, prior_document: str, scope: str) -> bool:
    """Only a trust-snapshot comparison; NOT an authorization to commit."""
    return (binding.trust_revision == current.revision
            and binding.trust_digest == current.digest()
            and binding.policy_version == current.policy_version
            and binding.claim_digest == claim_digest
            and binding.prior_document == prior_document
            and binding.scope == scope)
