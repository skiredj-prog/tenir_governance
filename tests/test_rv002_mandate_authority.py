"""RV-002 authority-store contract and ordering tests (sandbox, no network)."""
import tempfile
import threading
import unittest
from pathlib import Path

from tenir_governance.operations.mandate_authority import SQLiteMandateAuthority


class MandateAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "rv002.sqlite3")
        self.auth = SQLiteMandateAuthority(self.db)
        self.kw = dict(mandate_id="m-1", actor_sub="agent-1",
                       represented_user_sub="human-1", bearer_token_ref="token-1")
        self.ctx = dict(self.kw)
        self.auth.register_mandate(**self.kw)
        self.auth.bind_operation(
            operation_id="op-1", payload_sha256="a" * 64, **self.kw,
        )

    def claim(self, op="op-1", actor="agent-1", digest="a"*64, ctx=None):
        return self.auth.claim_fence(
            operation_id=op, actor_sub=actor, payload_sha256=digest,
            authority_context=self.ctx if ctx is None else ctx,
        )

    def test_valid_claim(self):
        with self.claim() as check:
            self.assertTrue(check.allowed)
            self.assertEqual("ALLOW_CLAIM", check.decision)
            check.claim_succeeded = True
        self.assertEqual(
            ["mandate_created", "operation_bound", "release_check", "dispatch_claim"],
            [e["kind"] for e in self.auth.audit_events()],
        )

    def test_revoked_before_claim(self):
        r = self.auth.revoke_mandate("m-1")
        with self.claim() as check:
            self.assertFalse(check.allowed)
            self.assertEqual("DENY_REVOKED", check.decision)
        ev = self.auth.audit_events()
        self.assertLess(r["seq"], ev[-1]["seq"])
        self.assertFalse(any(e["kind"] == "dispatch_claim" for e in ev))

    def test_revocation_is_idempotent(self):
        a = self.auth.revoke_mandate("m-1")
        b = self.auth.revoke_mandate("m-1")
        self.assertEqual(a, b)

    def test_missing_binding_and_context(self):
        with self.claim(op="unknown") as c:
            self.assertEqual("DENY_NO_BINDING", c.decision)
        with self.claim(ctx={}) as c:
            self.assertEqual("DENY_CONTEXT_MISMATCH", c.decision)

    def test_actor_and_payload_swaps(self):
        with self.claim(actor="agent-2") as c:
            self.assertEqual("DENY_ACTOR_MISMATCH", c.decision)
        with self.claim(digest="b"*64) as c:
            self.assertEqual("DENY_PAYLOAD_MISMATCH", c.decision)

    def test_mandate_and_delegation_swaps(self):
        for key, value in [
            ("mandate_id", "m-2"),
            ("represented_user_sub", "other"),
            ("bearer_token_ref", "other-token"),
        ]:
            ctx = dict(self.ctx, **{key: value})
            with self.claim(ctx=ctx) as c:
                self.assertEqual("DENY_CONTEXT_MISMATCH", c.decision)

    def test_new_instance_sees_revocation(self):
        SQLiteMandateAuthority(self.db).revoke_mandate("m-1")
        with self.claim() as c:
            self.assertEqual("DENY_REVOKED", c.decision)

    def test_concurrent_revocation_cannot_interleave_claim(self):
        entered = threading.Event()
        completed = threading.Event()
        results = []
        def revoke():
            entered.set()
            results.append(SQLiteMandateAuthority(self.db).revoke_mandate("m-1"))
            completed.set()
        # Claim serializes ahead of revocation; revocation ACK cannot race
        # between the native claim and the releasing transaction.
        with self.claim() as claim:
            self.assertTrue(claim.allowed)
            t = threading.Thread(target=revoke, daemon=True)
            t.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(completed.wait(0.1))
            claim.claim_succeeded = True
        self.assertTrue(completed.wait(5))
        t.join(timeout=2)
        self.assertGreater(results[0]["seq"], claim.sequence)
        # After revocation ACK, a new attempt must refuse.
        with self.claim() as later:
            self.assertEqual("DENY_REVOKED", later.decision)

    def test_fail_closed_unavailable_db(self):
        # SQLite operational errors are intentionally NOT swallowed at the
        # authority-store layer; the Governor catches and returns a DENY.
        Path(self.db).unlink()
        Path(self.db).mkdir()
        with self.assertRaises(Exception):
            with self.claim():
                pass


if __name__ == "__main__":
    unittest.main()
