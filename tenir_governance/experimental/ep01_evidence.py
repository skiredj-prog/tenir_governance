"""Experimental EP-01 evidence preflight, detached from runtime authorization.

Cryptographic origin and delegated scope are independent checks. The trust
registry is caller supplied; production integration must secure its origin.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tenir_governance.experimental.ep01_policy import Claim, Verdict, evaluate


@dataclass(frozen=True)
class SignedClaim:
    claim: Claim
    signature: bytes


def canonical_claim_bytes(claim: Claim) -> bytes:
    import json
    if claim.effective_at.tzinfo is None or claim.effective_at.utcoffset() is None:
        raise ValueError("effective_at must include timezone")
    return json.dumps({
        "document_id": claim.identifier,
        "issuer_id": claim.issuer,
        "replaces": claim.replaces,
        "effective_at": claim.effective_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_and_evaluate(
    signed: tuple[SignedClaim, ...],
    *, prior: str, scope: str,
    keys: Mapping[str, Ed25519PublicKey],
    mandates: Mapping[str, frozenset[str]],
    now: datetime,
) -> tuple[Verdict, str]:
    """Fail closed before pure-policy evaluation; never authorizes a commit."""
    if not signed:
        return Verdict.HOLD, "INSUFFICIENT_CONTEXT"
    for item in signed:
        public = keys.get(item.claim.issuer)
        if not isinstance(public, Ed25519PublicKey):
            return Verdict.REJECT, "UNKNOWN_ISSUER_KEY"
        try:
            public.verify(item.signature, canonical_claim_bytes(item.claim))
        except (InvalidSignature, ValueError, TypeError):
            return Verdict.REJECT, "INVALID_SIGNATURE"
    return evaluate(tuple(item.claim for item in signed), prior=prior,
                    scope=scope, mandates=dict(mandates), now=now)
