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
    """The native mock already records every execute() attempt."""
    def __init__(self):
        super().__init__(behavior="confirmed")


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


    def test_B1_mixed_configuration_cannot_dispatch_protected_operation(self):
        """Same durable registry, separate Governor without mandate authority."""
        from tenir_governance.operations.registry import OperationState
        self.admit("b1")
        self.auth.revoke_mandate("m1")
        legacy = DualKernelCompositionClient(
            store_path=self.registry_path, require_persistence=True,
        )
        legacy.register_agent(Agent(
            agent_id=CONTEXT["actor_sub"], authority_score=0.9,
        ))
        legacy.register_resource(Resource(
            resource_id="rv2-budget", capacity=100.0,
        ))
        provider = CountingProvider()
        legacy.governor.provider = provider
        result = self.release("b1", client=legacy)
        self.assertFalse(result.get("dispatched"), result)
        self.assertEqual([], provider.calls)
        self.assertEqual(
            "reserved", legacy.governor.registry.get("b1").state.value,
        )
        # No direct native registry path may bypass strict mode.
        success, _ = legacy.governor.registry.transition_to(
            "b1", OperationState.DISPATCHING, claimed_by="unguarded"
        )
        self.assertFalse(success)
        self.assertEqual([], provider.calls)

    def test_B1_unguarded_governor_created_before_strict_activation(self):
        """An already-open legacy client must observe later strict enrollment."""
        # This must be a separate registry: setUp already initialized strict
        # mode on self.registry_path.
        from tenir_governance.operations.registry import OperationState
        alt_path = str(Path(self.tmp.name) / "legacy-first-registry.json")
        legacy = DualKernelCompositionClient(
            store_path=alt_path, require_persistence=True,
        )
        legacy.register_agent(Agent(
            agent_id=CONTEXT["actor_sub"], authority_score=0.9,
        ))
        legacy.register_resource(Resource(
            resource_id="rv2-budget", capacity=100.0,
        ))
        provider = CountingProvider()
        legacy.governor.provider = provider

        # Strict enrollment occurs AFTER the unguarded instance was created.
        protected = DualKernelCompositionClient(
            store_path=alt_path, require_persistence=True,
            mandate_authority=self.auth,
        )
        protected.register_agent(Agent(
            agent_id=CONTEXT["actor_sub"], authority_score=0.9,
        ))
        protected.register_resource(Resource(
            resource_id="rv2-budget", capacity=100.0,
        ))
        passage = Passage(
            agent_id=CONTEXT["actor_sub"], action_type="get_system_time",
            amount=1.0, idempotency_key="b1old",
        )
        d = protected.governor.adjudicate(
            passage, pressure=0.7, velocity=0.5, capacity=0.9,
            resource_id="rv2-budget", amount=1.0, payload=PAYLOAD,
            authority=0.9, context_fit=1.0, evidence_quality=1.0,
        )
        self.assertEqual("reserved", d["operation_state"])
        self.auth.bind_operation(
            operation_id="b1old",
            payload_sha256=canonical_payload_hash(PAYLOAD), **CONTEXT,
        )
        self.auth.revoke_mandate("m1")
        result = legacy.governor.dispatch_and_execute(
            "b1old", resource_id="rv2-budget", amount=1.0,
            payload=PAYLOAD,
        )
        self.assertFalse(result.get("dispatched"), result)
        self.assertEqual([], provider.calls)
        success, _ = legacy.governor.registry.transition_to(
            "b1old", OperationState.DISPATCHING, claimed_by="unguarded"
        )
        self.assertFalse(success)

    def test_B1_resume_advice_requires_authority_recheck(self):
        self.admit("b1resume")
        self.auth.revoke_mandate("m1")
        resume = self.client.resume_existing("b1resume")
        self.assertEqual("revalidate_mandate", resume["action"], resume)
        self.assertNotIn("Safe to dispatch", resume["reason"])

    def test_B1_fabricated_claim_cannot_bypass_revocation(self):
        import sqlite3
        from tenir_governance.operations.registry import OperationState
        from tenir_governance.operations.mandate_authority import MandateClaim
        self.admit("b1forged")
        self.auth.revoke_mandate("m1")
        with sqlite3.connect(self.auth.path, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            forged = MandateClaim(
                allowed=True, decision="ALLOW_CLAIM", sequence=123,
                epoch=1, operation_id="b1forged",
                authority_path=self.auth.path, active=True, connection=db,
            )
            success, _ = self.client.governor.registry.transition_to(
                "b1forged", OperationState.DISPATCHING,
                claimed_by="forged", mandate_claim=forged,
            )
            self.assertFalse(success)
            db.rollback()
        self.assertEqual("reserved", self.client.governor.registry.get("b1forged").state.value)

    def test_C2_simulated_crash_after_durable_registry_claim(self):
        """Exception-boundary injection, not independent SIGKILL evidence."""
        from tenir_governance.operations.registry import OperationState
        self.admit("c2sim")
        registry = self.client.governor.registry
        original_transition = registry.transition_to

        def kill_after_persist(*args, **kwargs):
            outcome = original_transition(*args, **kwargs)
            if args[1] == OperationState.DISPATCHING and outcome[0]:
                raise SystemExit("INJECTED_CRASH_AFTER_REGISTRY_PERSISTENCE")
            return outcome

        registry.transition_to = kill_after_persist
        with self.assertRaisesRegex(SystemExit, "INJECTED_CRASH"):
            self.release("c2sim")
        registry.transition_to = original_transition
        self.assertEqual("dispatching", registry.get("c2sim").state.value)
        self.assertFalse(any(
            e["kind"] == "dispatch_claim"
            for e in self.auth.audit_events("c2sim")
        ))
        self.assertEqual([], self.provider.calls)
        fresh_provider = CountingProvider()
        fresh = self.new_client(fresh_provider)
        decision = fresh.resume_existing("c2sim")
        self.assertEqual("reconcile", decision["action"], decision)
        attempted = self.release("c2sim", client=fresh)
        self.assertFalse(attempted.get("dispatched"), attempted)
        self.assertEqual([], fresh_provider.calls)

    def test_B1_strict_mark_removal_fails_closed(self):
        """If a strict marker disappears, it must not restore legacy dispatch."""
        from pathlib import Path
        self.admit("b1mark")
        Path(self.registry_path + ".mandate.strict.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "strict mark"):
            self.client.governor.registry.requires_mandate_claim("b1mark")
        # Even the unguarded instance has no authorized execution route.
        legacy = DualKernelCompositionClient(
            store_path=self.registry_path, require_persistence=True,
        )
        provider = CountingProvider()
        legacy.governor.provider = provider
        with self.assertRaisesRegex(RuntimeError, "strict mark"):
            legacy.governor.registry.transition_to(
                "b1mark", __import__(
                    "tenir_governance.operations.registry",
                    fromlist=["OperationState"]
                ).OperationState.DISPATCHING,
            )
        self.assertEqual([], provider.calls)



if __name__ == "__main__":
    unittest.main()
