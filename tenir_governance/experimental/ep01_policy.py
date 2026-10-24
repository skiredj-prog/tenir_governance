"""EP-01 documentary authority policy, experimental and not wired to execution."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Verdict(str, Enum):
    ADMISSIBLE = "ADMISSIBLE"
    HOLD = "HOLD"
    REJECT = "REJECT"


@dataclass(frozen=True)
class Claim:
    identifier: str
    issuer: str
    replaces: str
    effective_at: datetime


def evaluate(claims: tuple[Claim, ...], *, prior: str, scope: str,
             mandates: dict[str, frozenset[str]], now: datetime) -> tuple[Verdict, str]:
    """Caller must authenticate claims before calling; this does not verify signatures."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now requires timezone")
    if not claims or not prior or not scope:
        return Verdict.HOLD, "INSUFFICIENT_CONTEXT"
    if any(not c.identifier or not c.issuer or not c.replaces
           or c.effective_at.tzinfo is None or c.effective_at.utcoffset() is None
           for c in claims):
        return Verdict.HOLD, "INVALID_INPUT"
    if len({c.identifier for c in claims}) != len(claims):
        return Verdict.HOLD, "DUPLICATE_CLAIM"
    if any(scope not in mandates.get(c.issuer, frozenset()) for c in claims):
        return Verdict.REJECT, "INSUFFICIENT_STANDING"
    if any(c.replaces != prior for c in claims):
        return Verdict.HOLD, "PRECEDENCE_UNRESOLVED"
    active = [c for c in claims if c.effective_at <= now]
    if len(active) > 1:
        return Verdict.HOLD, "CONFLICTING_ACTIVE_CLAIMS"
    if not active:
        return Verdict.HOLD, "NOT_YET_EFFECTIVE"
    return Verdict.ADMISSIBLE, "POLICY_PRECONDITIONS_MET"
