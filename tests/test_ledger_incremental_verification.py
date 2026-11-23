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
import copy
import threading
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
    assert provider.calls == []                      # the provider was never called
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


def test_a_direct_dispatch_call_also_refuses_on_a_ledger_that_does_not_verify():
    """Author's decision of 2026-11-23, 14:54 UTC (option A, after the human
    review of 14:42): the full verification lives in dispatch(), under the
    quarantine gate, right before the DISPATCHING claim. A caller that skips
    dispatch_and_execute and claims directly is covered too."""
    gov, provider = _dispatching_governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(p, **M_OK, payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
    gov.ledger.entries[0].rationale = "rewritten"
    d = gov.dispatch(p.idempotency_key, "budget", 10.0, payload={"action_type": "payment"},
                     flag_conditions_met=ACK)
    assert d["dispatched"] is False and d.get("ledger_integrity_failed") is True
    assert d["audit_entry_chain_proven"] is False     # declared: the audit entry extends a broken chain
    assert gov.registry.get(p.idempotency_key).state.value == "reserved", "no claim was taken"
    assert gov.ledger.entries[-1].kernel == "api" and gov.ledger.entries[-1].decision == "HARD_VETO"
    assert gov.ledger.verify_chain() is False          # still broken: the refusal does not repair anything


def test_the_sdk_client_dispatch_is_covered_by_the_full_verification():
    """The SDK client is the deployment surface; its dispatch() goes through
    the governor's dispatch() and therefore through the verification."""
    import warnings
    from tenir_governance.objects import Resource
    from tenir_governance.sdk import DualKernelCompositionClient
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = DualKernelCompositionClient(policy=CompositionPolicy(
            flag_conditions=("ops supervision",),
            hold_tau_k_seconds=3600.0, hold_tau_k_source="test fixture: one hour, not an SLA"))
    client.register_agent(Agent(agent_id="alice", authority_score=0.9))
    client.register_resource(Resource(resource_id="budget", capacity=100.0))
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    r = client.adjudicate(p, **M_OK, resource_id="budget", amount=10.0)
    assert r["composed_decision"] in ("PASS", "FLAG")
    client.governor.ledger.entries[0].rationale = "rewritten"
    d = client.dispatch(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)
    assert d["dispatched"] is False and d.get("ledger_integrity_failed") is True
    assert client.governor.registry.get(p.idempotency_key).state.value == "reserved"


def test_an_intact_ledger_lets_a_direct_dispatch_claim():
    gov, _ = _dispatching_governor()
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(p, **M_OK, payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
    d = gov.dispatch(p.idempotency_key, "budget", 10.0, payload={"action_type": "payment"},
                     flag_conditions_met=ACK)
    assert d["dispatched"] is True
    assert gov.ledger.verify_chain() is True


# -- project owner's review of 2026-11-23, 17:37 UTC: concurrency, incident, order --

def test_concurrent_appends_do_not_fork_the_chain():
    """Eight threads adjudicating on one governor: the chain still verifies and
    holds every entry. Before the lock the chain forked in 9 runs out of 12."""
    state = MembraneState()
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=CompositionPolicy(flag_conditions=("ops supervision",)))
    before = gov.ledger.entry_count
    errors = []

    def work(i):
        try:
            for k in range(25):
                gov.adjudicate(Passage(agent_id="alice", action_type="read"), **M_OK)
        except Exception as e:                       # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert gov.ledger.verify_chain() is True
    assert gov.ledger.verify_new_entries() is True
    per_decision = (gov.ledger.entry_count - before) // 200
    assert per_decision >= 1 and (gov.ledger.entry_count - before) == 200 * per_decision


def test_concurrent_raw_appends_keep_every_link():
    ledger = HashChainedLedger()

    def work(i):
        for k in range(200):
            ledger.append(kernel="t", decision="PASS", rationale=f"{i}-{k}")
    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.entry_count == 1600 and ledger.verify_chain() is True


def test_one_audit_entry_per_integrity_incident_and_the_incident_closes_on_a_verified_chain():
    gov, _ = _dispatching_governor()
    keys = []
    for _ in range(3):
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, **M_OK, payload={"action_type": "payment"}, resource_id="budget", amount=10.0)
        keys.append(p.idempotency_key)
    original = gov.ledger.entries[0].rationale
    gov.ledger.entries[0].rationale = "rewritten"
    n = gov.ledger.entry_count
    pay = {"action_type": "payment"}
    first = gov.dispatch(keys[0], "budget", 10.0, payload=pay, flag_conditions_met=ACK)
    second = gov.dispatch(keys[1], "budget", 10.0, payload=pay, flag_conditions_met=ACK)
    assert first["ledger_integrity_failed"] and second["ledger_integrity_failed"]
    assert first["incident_recorded"] is True and second["incident_recorded"] is False
    assert second["incident_refusals"] == 2
    vetoes = [e for e in gov.ledger.entries[n:] if "LEDGER_INTEGRITY_FAILED" in e.rationale]
    assert len(vetoes) == 1 and vetoes[0].kernel == "api"   # one api/HARD_VETO entry, not two
    assert gov.ledger_integrity_incident["refusals"] == 2
    assert all(gov.registry.get(k).state.value == "reserved" for k in keys)
    # Repair (the test puts the entry back): verify_ledger() closes the incident
    # and the next operation claims.
    gov.ledger.entries[0].rationale = original
    assert gov.verify_ledger() is True and gov.ledger_integrity_incident is None
    assert gov.dispatch(keys[2], "budget", 10.0, payload=pay, flag_conditions_met=ACK)["dispatched"] is True


def test_a_repaired_chain_closes_the_incident_at_the_claim_and_a_later_alteration_opens_a_new_one():
    """Owner's decision of 2026-11-23, 21:25 UTC (option A), on finding N1 of his
    review of 20:03: before it, only verify_ledger() closed an incident, so after
    a repair a second, distinct alteration was refused without writing anything
    and one audit entry covered two alterations."""
    gov, _ = _dispatching_governor()
    pay = {"action_type": "payment"}
    keys = []
    for _ in range(3):
        p = Passage(agent_id="alice", action_type="payment", amount=10.0)
        gov.adjudicate(p, **M_OK, payload=pay, resource_id="budget", amount=10.0)
        keys.append(p.idempotency_key)
    vetoes = lambda: [e for e in gov.ledger.entries
                      if e.kernel == "api" and "LEDGER_INTEGRITY_FAILED" in e.rationale]
    original = gov.ledger.entries[0].rationale
    gov.ledger.entries[0].rationale = "rewritten"
    first = gov.dispatch(keys[0], "budget", 10.0, payload=pay, flag_conditions_met=ACK)
    assert first["ledger_integrity_failed"] and first["incident_recorded"] is True
    assert len(vetoes()) == 1 and gov.ledger_integrity_incident is not None

    gov.ledger.entries[0].rationale = original                      # the chain verifies again
    claimed = gov.dispatch(keys[1], "budget", 10.0, payload=pay, flag_conditions_met=ACK)
    assert claimed["dispatched"] is True
    assert gov.ledger_integrity_incident is None, "a claim on a verified chain closes the incident"
    assert len(vetoes()) == 1, "closing writes nothing to the ledger"

    gov.ledger.entries[1].rationale = "rewritten again"             # a second, distinct alteration
    second = gov.dispatch(keys[2], "budget", 10.0, payload=pay, flag_conditions_met=ACK)
    assert second["ledger_integrity_failed"] is True
    assert second["incident_recorded"] is True and second["incident_refusals"] == 1
    assert len(vetoes()) == 2, "a new incident writes its own audit entry"
    assert gov.registry.get(keys[2]).state.value == "reserved"


def test_an_incident_still_closes_on_an_explicit_audit():
    gov, _ = _dispatching_governor()
    pay = {"action_type": "payment"}
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    gov.adjudicate(p, **M_OK, payload=pay, resource_id="budget", amount=10.0)
    original = gov.ledger.entries[0].rationale
    gov.ledger.entries[0].rationale = "rewritten"
    assert gov.dispatch(p.idempotency_key, "budget", 10.0, payload=pay,
                        flag_conditions_met=ACK)["ledger_integrity_failed"] is True
    assert gov.verify_ledger() is False and gov.ledger_integrity_incident is not None
    gov.ledger.entries[0].rationale = original
    assert gov.verify_ledger() is True and gov.ledger_integrity_incident is None


def test_a_profile_s_authorization_is_not_consumed_when_the_ledger_does_not_verify(monkeypatch):
    """The single-use commit authorization is consumed only after the ledger
    verified; a refusal leaves it intact (regression of the earlier candidate
    state, which consumed first)."""
    from hashlib import sha256
    from tenir_governance.experimental.ep02_commit_authorization import canonical_payload_bytes
    from tenir_governance.operations import OperationRecord, OperationState
    gov, _ = _dispatching_governor()
    consumed = []
    monkeypatch.setattr(gov, "_consume_commit_authorization",
                        lambda key: consumed.append(key) or {"consumed": True})
    rec = OperationRecord(idempotency_key="gated", passage_id="p", agent_id="alice",
                          action_type="payment", commit_gate_required=True,
                          payload_digest=sha256(canonical_payload_bytes({})).hexdigest())
    assert gov.registry.register(rec)
    assert gov.registry.transition_to("gated", OperationState.RESERVED)[0]
    gov.ledger.append(kernel="t", decision="PASS", rationale="x")
    gov.ledger.entries[-1].rationale = "rewritten"
    d = gov.dispatch("gated", payload={})
    assert d["dispatched"] is False and d["ledger_integrity_failed"] is True
    assert consumed == []                                  # nothing consumed on a refusal
    assert gov.registry.get("gated").state is OperationState.RESERVED


def test_a_ledger_can_be_deep_copied_and_pickled_and_the_copy_has_its_own_lock():
    """Found by the REG harness on the first locked candidate: CT-R4-008 and
    CT-R5-004 snapshot the ledger and failed on `cannot pickle RLock`."""
    import copy
    import pickle
    ledger = HashChainedLedger()
    for i in range(3):
        ledger.append(kernel="t", decision="PASS", rationale=f"r{i}")
    snap = copy.deepcopy(ledger)
    back = pickle.loads(pickle.dumps(ledger))
    for other in (snap, back):
        assert other.verify_chain() is True and other.entry_count == 3
        assert other._lock is not ledger._lock
    ledger.append(kernel="t", decision="PASS", rationale="after")
    assert snap.entry_count == 3 and ledger.entry_count == 4          # independent copies
    assert snap.verify_chain() is True and ledger.verify_chain() is True

