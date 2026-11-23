"""verify_chain, option A of the author's decision of 2026-11-23 (candidate
5.7.7, not released): the per-decision check of the ledger is incremental, the
full verification stays available on demand.

Integrity properties this file proves:
- an incremental verification accepts exactly what the full one accepts on an
  untouched ledger, and agrees on the prefix it verified;
- the alteration of a new link (an entry appended since the last verification)
  is detected incrementally, before the full verification runs;
- the alteration of the anchor (the last verified entry) is detected
  incrementally;
- a truncated prefix is detected incrementally;
- the alteration of an old entry inside the verified prefix is detected by the
  full verification (the incremental check's declared limit);
- the cost of the incremental check does not grow with the ledger.
"""
import time

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.lattice import HashChainedLedger
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage
from tenir_kernel.kernel import TenirKernel


def _ledger(n):
    ledger = HashChainedLedger()
    for i in range(n):
        ledger.append("scoring", "PASS", f"entry {i}", passage_id=f"p{i}")
    return ledger


def test_empty_ledger_verifies_both_ways():
    ledger = HashChainedLedger()
    assert ledger.verify_new_entries() is True
    assert ledger.verify_chain() is True
    assert ledger.verified_count == 0


def test_incremental_covers_the_new_entries_and_agrees_with_the_full_check():
    ledger = _ledger(5)
    assert ledger.verified_count == 0
    assert ledger.verify_new_entries() is True
    assert ledger.verified_count == 5
    ledger.append("membrane", "PASS", "six")
    ledger.append("composed", "PASS", "seven")
    assert ledger.verify_new_entries() is True
    assert ledger.verified_count == 7
    assert ledger.verify_chain() is True
    assert ledger.verified_count == 7
    # Nothing new: still true, still anchored.
    assert ledger.verify_new_entries() is True


def test_a_tampered_new_link_is_detected_incrementally():
    ledger = _ledger(3)
    assert ledger.verify_new_entries() is True
    ledger.append("composed", "PASS", "admitted")
    ledger.entries[-1].decision = "HARD_VETO"  # content changed, hash not recomputed
    assert ledger.verify_new_entries() is False
    assert ledger.verify_chain() is False


def test_a_new_link_rehashed_but_unlinked_is_detected_incrementally():
    """An attacker rewrites a new entry and its own hash: the next entry's
    previous_hash no longer matches, and so does the anchor's hash."""
    ledger = _ledger(3)
    assert ledger.verify_new_entries() is True
    ledger.append("composed", "PASS", "admitted")
    ledger.append("composed", "PASS", "second")
    forged = ledger.entries[3]
    forged.decision = "HARD_VETO"
    forged.chain_hash = HashChainedLedger._entry_hash(forged)
    assert ledger.verify_new_entries() is False


def test_a_tampered_anchor_is_detected_incrementally():
    ledger = _ledger(4)
    assert ledger.verify_new_entries() is True
    ledger.entries[3].rationale = "rewritten"  # the last verified entry
    ledger.append("composed", "PASS", "later")
    assert ledger.verify_new_entries() is False


def test_a_replaced_anchor_is_detected_incrementally():
    ledger = _ledger(4)
    assert ledger.verify_new_entries() is True
    other = _ledger(4)
    ledger.entries[3] = other.entries[3]  # consistent hash, different history
    assert ledger.verify_new_entries() is False


def test_a_truncated_prefix_is_detected_incrementally():
    ledger = _ledger(4)
    assert ledger.verify_new_entries() is True
    del ledger.entries[2:]
    assert ledger.verify_new_entries() is False


def test_an_old_entry_altered_inside_the_prefix_is_the_declared_limit():
    """Not seen incrementally (it is the declared limit), seen by the full check."""
    ledger = _ledger(5)
    assert ledger.verify_new_entries() is True
    ledger.entries[1].decision = "HARD_VETO"
    ledger.append("composed", "PASS", "later")
    assert ledger.verify_new_entries() is True  # declared limit
    assert ledger.verify_chain() is False       # caught on demand
    # A failed full check does not move the verified prefix.
    assert ledger.verified_count == 6


def test_incremental_cost_does_not_grow_with_the_ledger():
    small = _ledger(50)
    big = _ledger(5000)
    for ledger in (small, big):
        assert ledger.verify_new_entries() is True

    def one_decision(ledger):
        t = time.perf_counter()
        for _ in range(200):
            ledger.append("composed", "PASS", "x")
            assert ledger.verify_new_entries() is True
        return time.perf_counter() - t

    t_small, t_big = one_decision(small), one_decision(big)
    # Same work per decision (one entry hashed, one anchor rehashed), so the
    # two timings are of the same order; a full check would be 100x apart.
    assert t_big < 5 * t_small + 0.05, (t_small, t_big)


def test_governor_reports_the_incremental_check_and_offers_the_full_one():
    state = MembraneState()
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=CompositionPolicy(flag_conditions=("ops supervision",)))
    m = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.2, capacity=0.9,
             option_space=0.9, authority=0.9, context_fit=0.9, evidence_quality=0.9)
    r = gov.adjudicate(Passage(agent_id="alice", action_type="read"), **m)
    assert r["ledger_valid"] is True
    assert gov.ledger.verified_count == gov.ledger.entry_count
    assert gov.verify_ledger() is True
    gov.ledger.entries[0].rationale = "rewritten"
    r2 = gov.adjudicate(Passage(agent_id="alice", action_type="read"), **m)
    assert r2["ledger_valid"] is True   # old entry: declared limit of the per-decision check
    assert gov.verify_ledger() is False  # full verification on demand


def _dispatching_governor(**policy_kw):
    from tenir_governance.objects import Resource
    from tenir_governance.operations import MockProvider
    state = MembraneState()
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    provider = MockProvider()
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=CompositionPolicy(flag_conditions=("ops supervision",), **policy_kw),
                             provider=provider)
    return gov, provider


M_OK = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.2, capacity=0.9,
            option_space=0.9, authority=0.9, context_fit=0.9, evidence_quality=0.9)
ACK = {"ops supervision": "acknowledged by the test"}


def test_full_verification_before_dispatch_closes_the_declared_limit():
    """An old entry altered inside the verified prefix is not seen per decision;
    it is seen at the effect boundary and the dispatch is refused, fail-closed."""
    gov, provider = _dispatching_governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = gov.adjudicate(p, **M_OK, payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
    assert r["composed_decision"] in ("PASS", "FLAG") and r["ledger_valid"] is True
    gov.ledger.entries[0].rationale = "rewritten"
    d = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, payload={"action_type": "payment"},
                                 flag_conditions_met=ACK)
    assert d["dispatched"] is False and d.get("ledger_integrity_failed") is True
    assert "LEDGER_INTEGRITY_FAILED" in d["reason"]
    assert provider.execute_calls == [] if hasattr(provider, "execute_calls") else True
    assert gov.registry.get(p.idempotency_key).state.value == "reserved", "no claim was taken"
    assert gov.ledger.entries[-1].kernel == "api"


def test_an_intact_ledger_dispatches_and_the_check_can_be_left_to_the_caller():
    gov, _ = _dispatching_governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(p, **M_OK, payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
    d = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, payload={"action_type": "payment"},
                                 flag_conditions_met=ACK)
    assert d["dispatched"] is True
    gov2, _ = _dispatching_governor(ledger_full_verification_at_dispatch=False)
    p2 = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov2.adjudicate(p2, **M_OK, payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
    gov2.ledger.entries[0].rationale = "rewritten"
    d2 = gov2.dispatch_and_execute(p2.idempotency_key, "budget", 10.0, payload={"action_type": "payment"},
                                   flag_conditions_met=ACK)
    assert d2["dispatched"] is True          # the caller took the full verification on itself
    assert gov2.verify_ledger() is False     # and it is available on demand


def test_policy_flag_must_be_a_bool():
    with pytest.raises(ValueError):
        CompositionPolicy(ledger_full_verification_at_dispatch="yes").validate()
