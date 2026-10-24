"""EP-01 pure-policy tests; not a proof of authenticated standing."""
from datetime import datetime, timezone, timedelta

from tenir_governance.experimental.ep01_policy import Claim, Verdict, evaluate


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
MANDATES = {"council": frozenset({"contract"}), "outsider": frozenset()}


def claim(doc="B", issuer="council", effective=NOW, replaces="A"):
    return Claim(doc, issuer, replaces, effective)


def decision(*claims):
    return evaluate(tuple(claims), prior="A", scope="contract",
                    mandates=MANDATES, now=NOW)


def test_ep01_cannot_assume_standing_from_a_document():
    assert decision(claim(issuer="outsider")) == (
        Verdict.REJECT, "INSUFFICIENT_STANDING")


def test_ep01_future_effect_does_not_activate():
    assert decision(claim(effective=NOW + timedelta(days=1))) == (
        Verdict.HOLD, "NOT_YET_EFFECTIVE")


def test_ep01_competing_authorized_claims_are_held():
    assert decision(claim("B"), claim("C")) == (
        Verdict.HOLD, "CONFLICTING_ACTIVE_CLAIMS")


def test_ep01_authorized_current_candidate_is_not_runtime_commit():
    assert decision(claim()) == (
        Verdict.ADMISSIBLE, "POLICY_PRECONDITIONS_MET")


def test_ep01_wrong_supersession_is_not_accepted():
    assert decision(claim(replaces="X")) == (
        Verdict.HOLD, "PRECEDENCE_UNRESOLVED")
