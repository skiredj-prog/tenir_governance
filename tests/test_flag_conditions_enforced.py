"""5.7.6, RFC-0 §4 (decision 3): a FLAG admits only under the conditions the
policy declares, and the dispatch of a FLAG-admitted operation requires every
condition to be acknowledged (audit reserve P1 "FLAG / option_space", 20 Nov).

- Default policy (no `flag_conditions`): a scoring FLAG composes to HOLD, the
  operation is held with its reservation, nothing is admitted.
- Declared conditions: the FLAG stands, the admission records the terms in the
  same registry write as RESERVED (`composed_decision`, `flag_conditions`).
- Dispatch without `flag_conditions_met` covering every condition is refused;
  the acknowledgement is persisted once and ledgered; a PASS admission ignores it.
- `option_space` unmeasured is insufficient evidence: HOLD, never PASS or FLAG.
- A HOLD re-admitted as FLAG starts with no acknowledgement.
"""
import warnings

import pytest

from tenir_governance.composition import DualKernelGovernor, ScoringKernelAdapter
from tenir_governance.composition.decisions import Decision
from tenir_governance.composition.policy import CompositionPolicy
from tenir_governance.membrane import MembraneState, TransitionMembrane
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations import MockProvider, OperationState
from tenir_governance.operations.registry import DurableOperationRegistry, OperationRecord
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_kernel.kernel import TenirKernel

CONDITIONS = ("human review of the effect within 24 h", "amount capped at 100")
ACK = {"human review of the effect within 24 h": "ticket OPS-12, reviewer dana",
       "amount capped at 100": "amount 10.0 checked by the pipeline"}
M = dict(irreversibility_materiality=0.0, pressure=0.2, velocity=0.3, capacity=1.0, authority=0.9, context_fit=0.9,
         evidence_quality=0.9, option_space=0.9)


def _gov(policy=None, *, store_path=None, provider=None):
    state = MembraneState()
    state.register_resource(Resource(resource_id="budget", capacity=100.0))
    state.register_agent(Agent(agent_id="alice", authority_score=0.9))
    gov = DualKernelGovernor(ScoringKernelAdapter(TenirKernel()), TransitionMembrane(state),
                             policy=policy, registry=DurableOperationRegistry(store_path=store_path),
                             provider=provider)
    return gov, state


def _admit(gov, amount=10.0, **overrides):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    r = gov.adjudicate(p, **{**M, **overrides}, resource_id="budget", amount=amount)
    return p, r


class _BrokenScoring:
    def adjudicate(self, **kwargs):
        raise RuntimeError("forced HOLD")


# --- admission --------------------------------------------------------------

def test_default_policy_holds_a_flag_instead_of_admitting_it():
    gov, state = _gov()
    p, r = _admit(gov)
    assert r["scoring_decision"] == Decision.FLAG.value
    assert r["membrane_decision"] == Decision.PASS.value
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["operation_state"] == OperationState.HOLD.value
    assert r["flag_conditions"] == []
    assert "FLAG states no condition" in r["composed_rationale"]
    assert state.resources["budget"].reserved == 10.0  # held with its reservation, not admitted
    composed = [e for e in gov.ledger.entries if e.kernel == "composed"][-1]
    assert composed.decision == Decision.HOLD.value and "RFC-0 §4" in composed.rationale


def test_default_policy_hold_dispatches_nothing():
    gov, state = _gov(provider=MockProvider())
    p, _ = _admit(gov)
    out = gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met={"x": "y"})
    assert out["dispatched"] is False
    assert gov.provider.calls == []
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD


def test_declared_conditions_admit_the_flag_and_record_the_terms(tmp_path):
    gov, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS), store_path=str(tmp_path / "ops.json"))
    p, r = _admit(gov)
    assert r["composed_decision"] == Decision.FLAG.value
    assert r["operation_state"] == OperationState.RESERVED.value
    assert r["flag_conditions"] == list(CONDITIONS)
    record = gov.registry.get(p.idempotency_key)
    assert record.composed_decision == Decision.FLAG.value
    assert record.flag_conditions == list(CONDITIONS)
    assert record.flag_conditions_met is None
    # Persisted in the same write as RESERVED: a fresh registry reads the terms.
    again = DurableOperationRegistry(store_path=str(tmp_path / "ops.json")).get(p.idempotency_key)
    assert again.state == OperationState.RESERVED and again.flag_conditions == list(CONDITIONS)


def test_pass_admission_records_no_condition():
    gov, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS))
    p = Passage(agent_id="alice", action_type="read")
    # No budget and a PASS from both kernels is not reachable with the four
    # never-measured axes; check the recorded terms through the registry API.
    reg = gov.registry
    reg.register(OperationRecord(idempotency_key="pass-op", passage_id="p", agent_id="alice", action_type="read"))
    assert reg.transition_to("pass-op", OperationState.RESERVED,
                             admission_terms={"composed_decision": "PASS", "flag_conditions": CONDITIONS})[0]
    record = reg.get("pass-op")
    assert record.composed_decision == "PASS" and record.flag_conditions == []
    assert gov.dispatch("pass-op", flag_conditions_met={"ignored": "for a PASS"})["dispatched"] is True


@pytest.mark.parametrize("terms", [
    {"composed_decision": "HOLD"}, {"composed_decision": "FLAG", "flag_conditions": ()},
    {"composed_decision": "FLAG", "flag_conditions": ("", "x")}, {"other": 1},
])
def test_registry_validates_admission_terms(terms):
    reg = DurableOperationRegistry()
    reg.register(OperationRecord(idempotency_key="op", passage_id="p", agent_id="a", action_type="t"))
    with pytest.raises(ValueError):
        reg.transition_to("op", OperationState.RESERVED, admission_terms=terms)
    assert reg.get("op").state == OperationState.CREATED


def test_admission_terms_apply_only_to_reserved_transitions():
    reg = DurableOperationRegistry()
    reg.register(OperationRecord(idempotency_key="op", passage_id="p", agent_id="a", action_type="t"))
    with pytest.raises(ValueError):
        reg.transition_to("op", OperationState.FAILED, admission_terms={"composed_decision": "PASS"})


# --- dispatch ---------------------------------------------------------------

def test_dispatch_of_a_flag_admission_requires_every_condition():
    gov, state = _gov(CompositionPolicy(flag_conditions=CONDITIONS), provider=MockProvider())
    p, _ = _admit(gov)
    key = p.idempotency_key
    refused = gov.dispatch(key, "budget", 10.0)
    assert refused["dispatched"] is False
    assert "not acknowledged" in refused["reason"]
    assert refused["flag_conditions"] == list(CONDITIONS)
    partial = gov.dispatch(key, "budget", 10.0, flag_conditions_met={CONDITIONS[0]: "ticket OPS-12"})
    assert partial["dispatched"] is False and "exactly the declared conditions" in partial["reason"]
    extra = gov.dispatch(key, "budget", 10.0, flag_conditions_met={**ACK, "another": "x"})
    assert extra["dispatched"] is False
    with_empty = gov.dispatch(key, "budget", 10.0, flag_conditions_met={**ACK, CONDITIONS[1]: "  "})
    assert with_empty["dispatched"] is False and "invalid" in with_empty["reason"]
    assert gov.registry.get(key).state == OperationState.RESERVED
    assert gov.registry.get(key).flag_conditions_met is None
    assert state.resources["budget"].reserved == 10.0 and state.resources["budget"].consumed == 0.0
    assert gov.provider.calls == []
    assert gov.resume_existing(key)["action"] == "acknowledge_flag_conditions"

    out = gov.dispatch_and_execute(key, "budget", 10.0, flag_conditions_met=ACK)
    assert out["dispatched"] is True and out["operation_state"] == OperationState.CONFIRMED.value
    record = gov.registry.get(key)
    assert record.flag_conditions_met == ACK
    acknowledged = [e for e in gov.ledger.entries
                    if e.idempotency_key == key and "FLAG conditions acknowledged at dispatch" in e.rationale]
    assert len(acknowledged) == 1 and all(v in acknowledged[0].rationale for v in ACK.values())
    assert state.resources["budget"].consumed == 10.0


def test_acknowledgement_is_recorded_once_and_a_replay_is_idempotent():
    gov, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS))
    p, _ = _admit(gov)
    key = p.idempotency_key
    assert gov.registry.record_flag_conditions_met(key, ACK) is True
    assert gov.registry.record_flag_conditions_met(key, ACK) is True  # same statement: idempotent
    assert gov.registry.record_flag_conditions_met(key, {**ACK, CONDITIONS[0]: "someone else"}) is False
    assert gov.registry.get(key).flag_conditions_met == ACK
    # The acknowledgement already recorded lets a dispatch without one proceed.
    assert gov.dispatch(key, "budget", 10.0)["dispatched"] is True
    assert sum("FLAG conditions acknowledged" in e.rationale for e in gov.ledger.entries) == 0


def test_acknowledgement_is_accepted_on_a_reserved_operation_only():
    gov, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS))
    p, _ = _admit(gov)
    assert gov.dispatch(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)["dispatched"]
    assert gov.registry.record_flag_conditions_met(p.idempotency_key, ACK) is True  # already recorded
    gov2, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS))
    p2, _ = _admit(gov2)
    assert gov2.fail(p2.idempotency_key, "budget", 10.0) is True  # RESERVED → FAILED
    assert gov2.registry.record_flag_conditions_met(p2.idempotency_key, ACK) is False


def test_acknowledgement_survives_a_restart(tmp_path):
    store = str(tmp_path / "ops.json")
    gov, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS), store_path=store)
    p, _ = _admit(gov)
    assert gov.registry.record_flag_conditions_met(p.idempotency_key, ACK)
    again = DurableOperationRegistry(store_path=store).get(p.idempotency_key)
    assert again.flag_conditions_met == ACK


def test_sdk_passes_the_acknowledgement_through():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = DualKernelCompositionClient(policy=CompositionPolicy(flag_conditions=CONDITIONS))
    client.register_agent(Agent(agent_id="alice", authority_score=0.9))
    client.register_resource(Resource(resource_id="budget", capacity=100.0))
    p = Passage(agent_id="alice", action_type="payment", amount=10.0)
    assert client.adjudicate(p, **M, resource_id="budget", amount=10.0)["composed_decision"] == "FLAG"
    assert client.dispatch(p.idempotency_key, "budget", 10.0)["dispatched"] is False
    assert client.dispatch(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)["dispatched"] is True


# --- option_space -----------------------------------------------------------

def test_unmeasured_option_space_is_held_not_admitted():
    gov, state = _gov(CompositionPolicy(flag_conditions=CONDITIONS))
    p, r = _admit(gov, option_space=None)
    assert r["scoring_decision"] == Decision.HOLD.value
    # 5.7.7 candidate (R7): an unknown option space takes the insufficient-
    # evidence path, like every other unknown measure.
    assert "absent measure(s) option_space" in r["scoring_rationale"]
    assert r["composed_decision"] == Decision.HOLD.value
    assert r["operation_state"] == OperationState.HOLD.value
    assert "option_space" in r["declared_unmeasured"]
    # The measure arrives: re-adjudication admits under the declared conditions.
    res = gov.resolve_hold(p.idempotency_key, passage=p, **M, resource_id="budget", amount=10.0)
    assert res["hold_resolved"] is True and res["composed_decision"] == Decision.FLAG.value
    assert state.resources["budget"].reserved == 10.0


def test_unmeasured_option_space_masks_no_conclusive_veto():
    gov, _ = _gov(CompositionPolicy(flag_conditions=CONDITIONS))
    p, r = _admit(gov, option_space=None, irreversibility_materiality=0.0, pressure=1.0, velocity=1.0, capacity=0.1)  # TAU breach
    assert r["scoring_decision"] == Decision.HARD_VETO.value
    assert r["composed_decision"] == Decision.HARD_VETO.value


# --- HOLD → RESERVED ----------------------------------------------------------

def test_hold_readmitted_as_flag_records_its_terms_and_needs_a_fresh_acknowledgement():
    gov, state = _gov(CompositionPolicy(flag_conditions=CONDITIONS), provider=MockProvider())
    gov.scoring = _BrokenScoring()
    p, r = _admit(gov)
    assert r["composed_decision"] == Decision.HOLD.value
    assert gov.registry.get(p.idempotency_key).flag_conditions == []
    gov.scoring = ScoringKernelAdapter(TenirKernel())
    res = gov.resolve_hold(p.idempotency_key, passage=p, **M, resource_id="budget", amount=10.0)
    assert res["hold_resolved"] is True and res["composed_decision"] == Decision.FLAG.value
    record = gov.registry.get(p.idempotency_key)
    assert record.composed_decision == Decision.FLAG.value
    assert record.flag_conditions == list(CONDITIONS) and record.flag_conditions_met is None
    assert gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0)["dispatched"] is False
    assert gov.dispatch_and_execute(p.idempotency_key, "budget", 10.0, flag_conditions_met=ACK)["dispatched"]
    assert state.resources["budget"].consumed == 10.0 and state.resources["budget"].reserved == 0.0


def test_hold_under_default_policy_stays_held_on_re_adjudication():
    gov, _ = _gov()
    p, r = _admit(gov)
    assert r["composed_decision"] == Decision.HOLD.value
    res = gov.resolve_hold(p.idempotency_key, passage=p, **M, resource_id="budget", amount=10.0)
    assert res["hold_resolved"] is False and res["composed_decision"] == Decision.HOLD.value
    assert "FLAG states no condition" in res["composed_rationale"]
    assert gov.registry.get(p.idempotency_key).state == OperationState.HOLD
