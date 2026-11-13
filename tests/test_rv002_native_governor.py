"""RV-002 native Governor boundary tests; requires the installed TENIR kernel.

These tests are to be executed independently by the RV-002 runner. No live
provider, no external network, no production mandate or credentials.
"""
from pathlib import Path
import tempfile
import unittest

from tenir_governance.sdk import DualKernelCompositionClient
from tenir_governance.objects import Agent, Passage, Resource
from tenir_governance.operations.mandate_authority import SQLiteMandateAuthority
from tenir_governance.operations.payload_binding import canonical_payload_hash
from tenir_governance.operations.provider import MockProvider


PAYLOAD = {
    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": {"name": "get_system_time", "arguments": {"timezone": "UTC"}},
}
CONTEXT = {
    "mandate_id": "m1",
    "actor_sub": "rv2-agent",
    "represented_user_sub": "rv2-human",
    "bearer_token_ref": "verified-token-ref",
}


class CountingProvider(MockProvider):
    def __init__(self):
        super().__init__(behavior="confirmed")
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return super().execute(**kwargs)


class NativeRV002Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry_path = str(root / "registry.json")
        self.auth = SQLiteMandateAuthority(str(root / "authority.sqlite3"))
        self.auth.register_mandate(**CONTEXT)
        self.provider = CountingProvider()
        self.client = self.new_client(self.provider)

    def new_client(self, provider):
        client = DualKernelCompositionClient(
            store_path=self.registry_path, require_persistence=True,
            mandate_authority=self.auth,
        )
        client.register_agent(Agent(
            agent_id=CONTEXT["actor_sub"], authority_score=0.9,
            trusted_delegates=frozenset(),
        ))
        client.register_resource(Resource(
            resource_id="rv2-budget", capacity=100.0,
        ))
        client.governor.provider = provider
        return client

    def admit(self, op):
        passage = Passage(
            agent_id=CONTEXT["actor_sub"], action_type="get_system_time",
            amount=1.0, idempotency_key=op,
        )
        decision = self.client.governor.adjudicate(
            passage, pressure=0.7, velocity=0.5, capacity=0.9,
            resource_id="rv2-budget", amount=1.0, payload=PAYLOAD,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
        )
        self.assertEqual("reserved", decision["operation_state"])
        self.auth.bind_operation(
            operation_id=op, payload_sha256=canonical_payload_hash(PAYLOAD),
            **CONTEXT,
        )

    def release(self, op, ctx=None, payload=None, client=None):
        return (client or self.client).governor.dispatch_and_execute(
            op, resource_id="rv2-budget", amount=1.0,
            payload=PAYLOAD if payload is None else payload,
            authority_context=CONTEXT if ctx is None else ctx,
        )

    def test_A1_valid_authority_one_provider_call(self):
        self.admit("a1")
        r = self.release("a1")
        self.assertTrue(r["dispatched"], r)
        self.assertEqual(1, len(self.provider.calls))
        self.assertFalse(self.release("a1").get("dispatched"))
        self.assertEqual(1, len(self.provider.calls))

    def test_A2_revocation_before_dispatch_zero_calls(self):
        self.admit("a2")
        ack = self.auth.revoke_mandate("m1")
        r = self.release("a2")
        self.assertEqual("DENY_REVOKED", r.get("guard_decision"), r)
        self.assertEqual(0, len(self.provider.calls))
        events = self.auth.audit_events("a2")
        self.assertLess(ack["seq"], events[-1]["seq"])
        self.assertFalse(any(e["kind"] == "dispatch_claim" for e in events))
        recovered = self.new_client(CountingProvider())
        rr = self.release("a2", client=recovered)
        self.assertFalse(rr.get("dispatched"), rr)
        self.assertEqual(0, len(recovered.governor.provider.calls))

    def test_A3_unknown_missing_context_and_swaps(self):
        self.admit("a3")
        r = self.release("a3", ctx={})
        self.assertEqual("DENY_CONTEXT_MISMATCH", r.get("guard_decision"), r)
        self.assertEqual(0, len(self.provider.calls))
        r = self.release("a3", ctx=dict(CONTEXT, actor_sub="other"))
        self.assertEqual("DENY_CONTEXT_MISMATCH", r.get("guard_decision"), r)
        self.assertEqual(0, len(self.provider.calls))

    def test_unbound_operation_fails_closed(self):
        passage = Passage(
            agent_id=CONTEXT["actor_sub"], action_type="get_system_time",
            amount=1.0, idempotency_key="unbound",
        )
        d = self.client.governor.adjudicate(
            passage, pressure=0.7, velocity=0.5, capacity=0.9,
            resource_id="rv2-budget", amount=1.0, payload=PAYLOAD,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
        )
        self.assertEqual("reserved", d["operation_state"])
        r = self.release("unbound")
        self.assertEqual("DENY_NO_BINDING", r.get("guard_decision"), r)
        self.assertEqual(0, len(self.provider.calls))

    def test_direct_sdk_dispatch_still_enforces_mandate(self):
        self.admit("direct")
        self.auth.revoke_mandate("m1")
        r = self.client.dispatch(
            "direct", "rv2-budget", 1.0, payload=PAYLOAD,
            authority_context=CONTEXT,
        )
        self.assertEqual("DENY_REVOKED", r.get("guard_decision"), r)
        self.assertEqual(0, len(self.provider.calls))


if __name__ == "__main__":
    unittest.main()
