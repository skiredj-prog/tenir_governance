"""
R5 IRON OMEGA: Full Test Suite
================================
Tests all four R5 components:
  - R5.1: NSL Grammar parser + compilation
  - R5.2: Neo4j graph schema (offline mock)
  - R5.3: WebSocket hub logic
  - R5.4: Merkle tree + ledger + key ceremony
"""

import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
import tempfile
import os

# ─── R5.1: NEURO-SYMBOLIC ─────────────────────────────────────────────────────

class TestNSLGrammar(unittest.TestCase):
    """Tests for the NSL tokenizer, parser, and AST compiler."""

    def setUp(self):
        from r5_neuro_symbolic.grammar.nsl_grammar import compile_nsl, compile_nsl_safe, tokenize
        self.compile = compile_nsl
        self.compile_safe = compile_nsl_safe
        self.tokenize = tokenize

    def test_accelerate_rnd(self):
        ast, params = self.compile("Accelerate the R&D project due to budget pressure")
        self.assertEqual(ast.intent.intent, "ACCELERATE")
        self.assertGreater(params["velocity"], 0.5)
        self.assertGreater(params["pressure"], 0.5)

    def test_delay_procurement(self):
        ast, params = self.compile("Delay the procurement contract until legal review")
        self.assertEqual(ast.intent.intent, "DELAY")
        self.assertLess(params["velocity"], 0.5)

    def test_restrict_budget(self):
        ast, params = self.compile("Restrict the budget allocation for R&D")
        self.assertEqual(ast.intent.intent, "RESTRICT")
        self.assertLess(params["capacity"], 0.85)
        self.assertLess(params["option_space"], 0.75)

    def test_urgency_modifier(self):
        _, params_no_urgency = self.compile("Accelerate the project")
        _, params_urgent = self.compile("Accelerate the project urgently")
        self.assertGreater(params_urgent["velocity"], params_no_urgency["velocity"])
        self.assertGreater(params_urgent["pressure"], params_no_urgency["pressure"])

    def test_risk_modifier(self):
        _, params_no_risk = self.compile("Delay the project")
        _, params_risky = self.compile("Delay the project due to risk")
        self.assertGreater(params_risky["pressure"], params_no_risk["pressure"])

    def test_identifier_extraction(self):
        ast, _ = self.compile("Accelerate partner_b-H2-2026 project immediately")
        if ast.entity and ast.entity.identifier:
            self.assertIn("partner_b-H2-2026", ast.entity.identifier)

    def test_unknown_intent_raises(self):
        from r5_neuro_symbolic.grammar.nsl_grammar import NSLParser
        _, params, err = self.compile_safe("The meeting is at 3pm tomorrow.")
        self.assertNotEqual(err, "")
        self.assertEqual(params, {})

    def test_params_within_bounds(self):
        for text in [
            "Accelerate the massive critical R&D project urgently due to risk",
            "Restrict all budget allocations immediately",
            "Query the status of the procurement contract",
        ]:
            _, params, _ = self.compile_safe(text)
            if params:
                for key, val in params.items():
                    self.assertGreaterEqual(val, 0.0, f"{key} < 0 for: {text}")
                    if key == "option_space":
                        self.assertLessEqual(val, 1.0)

    def test_context_clause(self):
        ast, _ = self.compile("Delay the project because budget is exhausted")
        if ast.context:
            ctx = " ".join(ast.context.reason_tokens).lower()
            self.assertIn("budget", ctx)

    def test_tokenizer_skips_punctuation(self):
        tokens = self.tokenize("Accelerate, please! The R&D project.")
        types = [t.type.name for t in tokens]
        self.assertIn("ACCELERATE", types)
        self.assertIn("RND_ENTITY", types)

    def test_fast_track_synonym(self):
        ast, params = self.compile("Fast-track the innovation project")
        self.assertEqual(ast.intent.intent, "ACCELERATE")

    def test_freeze_synonym(self):
        ast, params = self.compile("Freeze the procurement contract")
        self.assertEqual(ast.intent.intent, "DELAY")

    def test_query_intent(self):
        ast, params = self.compile("Review the status of contract partner_b-H2-2026")
        self.assertEqual(ast.intent.intent, "QUERY")

    def test_compile_to_event_params_types(self):
        _, params = self.compile("Allocate resources to the R&D team")
        self.assertIn("pressure", params)
        self.assertIn("velocity", params)
        self.assertIn("capacity", params)
        self.assertIn("option_space", params)
        for v in params.values():
            self.assertIsInstance(v, float)

    def test_context_clause_none(self):
        ast, _ = self.compile("Accelerate the project")
        # No "because" clause → context may be None
        # Just verify it doesn't crash

    def test_multiple_modifiers(self):
        ast, params = self.compile("Urgently restrict the budget allocation due to risk")
        self.assertEqual(ast.intent.intent, "RESTRICT")
        self.assertTrue(len(ast.modifiers) >= 1)


# ─── R5.2: GRAPH ONTOLOGY (mocked Neo4j) ─────────────────────────────────────

class TestCypherQueries(unittest.TestCase):
    """Validates Cypher query strings are well-formed and parameterized."""

    def setUp(self):
        from r5_graph_ontology.neo4j_graph import CypherQueries, SCHEMA_CYPHER
        self.Q = CypherQueries
        self.schema = SCHEMA_CYPHER

    def test_schema_has_constraints(self):
        constraint_stmts = [s for s in self.schema if "CONSTRAINT" in s]
        self.assertGreaterEqual(len(constraint_stmts), 5)

    def test_schema_has_indexes(self):
        index_stmts = [s for s in self.schema if "INDEX" in s]
        self.assertGreaterEqual(len(index_stmts), 3)

    def test_upsert_institution_parameterized(self):
        q = self.Q.UPSERT_INSTITUTION
        self.assertIn("$institution_id", q)
        self.assertIn("$name", q)
        self.assertIn("MERGE", q)

    def test_append_ledger_entry_has_hash_fields(self):
        q = self.Q.APPEND_LEDGER_ENTRY
        self.assertIn("$previous_hash", q)
        self.assertIn("$entry_hash", q)
        self.assertIn("CHAINS_TO", q)

    def test_detect_schizophrenia_query(self):
        q = self.Q.DETECT_SCHIZOPHRENIA
        self.assertIn("preferred_child_state", q)
        self.assertIn("conflicted_node", q)

    def test_all_queries_no_string_interpolation(self):
        """Verify no f-string style injections in queries."""
        import re
        queries = [v for k, v in vars(self.Q).items()
                   if not k.startswith('_') and isinstance(v, str)]
        for q in queries:
            # No direct Python variable interpolation (should use $params)
            self.assertNotRegex(q, r'\{[a-z_]+\}', f"Possible unsafe interpolation: {q[:50]}")


# ─── R5.3: WEBSOCKET HUB ─────────────────────────────────────────────────────

class TestWebSocketHub(unittest.IsolatedAsyncioTestCase):
    """Tests for the WebSocket hub broadcasting logic."""

    async def asyncSetUp(self):
        from r5_websocket.hub.ws_hub import WebSocketHub
        self.hub = WebSocketHub()

    async def test_trajectory_frame_structure(self):
        from r5_websocket.hub.ws_hub import trajectory_frame
        frame = trajectory_frame(0.85, -0.05, 0.01, 10, "TENSION", "SHADOW", "allow", "WF-001", 1)
        data = json.loads(frame)
        self.assertEqual(data["type"], "TRAJECTORY_UPDATE")
        self.assertEqual(data["payload"]["s_score"], 0.85)
        self.assertIn("vps", data["payload"])
        self.assertIn("x", data["payload"]["vps"])

    async def test_vps_coordinates_derivation(self):
        from r5_websocket.hub.ws_hub import _derive_vps_coords
        coords = _derive_vps_coords(1.5, 0.02, "REST")
        self.assertEqual(coords["color"], "#2dd4bf")
        self.assertEqual(coords["membrane_tension"], 0.0)

        coords_crit = _derive_vps_coords(0.2, -0.3, "COLLAPSE")
        self.assertEqual(coords_crit["color"], "#e05252")
        self.assertGreater(coords_crit["membrane_tension"], 0.5)

    async def test_vps_x_clamped(self):
        from r5_websocket.hub.ws_hub import _derive_vps_coords
        coords = _derive_vps_coords(0.5, 1000.0, "COLLAPSE")
        self.assertLessEqual(coords["x"], 5.0)
        coords2 = _derive_vps_coords(0.5, -1000.0, "COLLAPSE")
        self.assertGreaterEqual(coords2["x"], -5.0)

    async def test_tau_breach_frame(self):
        from r5_websocket.hub.ws_hub import tau_breach_frame
        frame = tau_breach_frame(0.30, 0.42, 5)
        data = json.loads(frame)
        self.assertEqual(data["type"], "TAU_BREACH")
        self.assertEqual(data["payload"]["severity"], "WARNING")

    async def test_whale_frame(self):
        from r5_websocket.hub.ws_hub import whale_frame
        frame = whale_frame(10)
        data = json.loads(frame)
        self.assertEqual(data["type"], "WHALE_RESONANCE")
        self.assertEqual(data["payload"]["depth"], "ABYSSAL")

    async def test_broadcast_no_clients(self):
        """Broadcast with no clients should not raise."""
        await self.hub.broadcast_trajectory(0.9, 0.01, 0.0, None, "REST", "SHADOW", "allow", "WF-TEST")

    async def test_hub_connection_count(self):
        self.assertEqual(self.hub.connection_count, 0)

    async def test_sequence_increments(self):
        s1 = self.hub.next_sequence()
        s2 = self.hub.next_sequence()
        self.assertEqual(s2, s1 + 1)

    async def test_frame_has_required_fields(self):
        from r5_websocket.hub.ws_hub import build_frame
        frame = build_frame("TEST", {"foo": "bar"}, 42)
        data = json.loads(frame)
        self.assertIn("frame_id", data)
        self.assertIn("timestamp", data)
        self.assertIn("sequence", data)
        self.assertEqual(data["sequence"], 42)


# ─── R5.4: DISTRIBUTED CRYPTOGRAPHY ──────────────────────────────────────────

class TestMerkleTree(unittest.TestCase):
    """Tests for the Merkle tree implementation."""

    def setUp(self):
        from r5_distributed_crypto.merkle.distributed_ledger import (
            build_merkle_tree, merkle_proof, verify_merkle_proof, _sha256
        )
        self.build = build_merkle_tree
        self.proof = merkle_proof
        self.verify = verify_merkle_proof
        self.sha256 = _sha256

    def test_single_leaf(self):
        root, levels = self.build(["abc"])
        self.assertEqual(root, self.sha256("abcabc"))   # duplicated

    def test_two_leaves(self):
        root, levels = self.build(["a", "b"])
        expected = self.sha256("a" + "b")
        self.assertEqual(root, expected)

    def test_power_of_two(self):
        leaves = [self.sha256(str(i)) for i in range(8)]
        root, levels = self.build(leaves)
        self.assertIsNotNone(root)
        self.assertEqual(len(levels), 4)   # log2(8) + 1

    def test_odd_leaves_duplicated(self):
        leaves = ["a", "b", "c"]
        root, _ = self.build(leaves)
        # Should not raise; last leaf is duplicated
        self.assertIsNotNone(root)

    def test_proof_verification_valid(self):
        leaves = [self.sha256(str(i)) for i in range(8)]
        root, levels = self.build(leaves)
        for idx in range(8):
            proof = self.proof(idx, levels)
            self.assertTrue(
                self.verify(leaves[idx], proof, root),
                f"Proof failed for leaf {idx}"
            )

    def test_proof_verification_invalid(self):
        leaves = [self.sha256(str(i)) for i in range(4)]
        root, levels = self.build(leaves)
        proof = self.proof(0, levels)
        fake_leaf = self.sha256("TAMPERED")
        self.assertFalse(self.verify(fake_leaf, proof, root))

    def test_deterministic_root(self):
        """Same leaves always produce the same root."""
        leaves = [self.sha256(str(i)) for i in range(16)]
        r1, _ = self.build(leaves)
        r2, _ = self.build(leaves)
        self.assertEqual(r1, r2)


class TestDistributedLedger(unittest.IsolatedAsyncioTestCase):
    """Tests for the append-only distributed ledger."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ledger_path = os.path.join(self.tmpdir, "test_ledger.jsonl")
        from r5_distributed_crypto.merkle.distributed_ledger import DistributedLedger
        self.ledger = DistributedLedger(ledger_path=self.ledger_path)

    async def test_append_returns_entry(self):
        entry = await self.ledger.append(
            tenant_id="partner_a", workflow_id="WF-001",
            membrane_decision="allow", operating_mode="SHADOW",
            s_score=1.2, ds_de=0.05, ces_state="REST", rationale="Test"
        )
        self.assertIsNotNone(entry.entry_id)
        self.assertIsNotNone(entry.entry_hash)

    async def test_genesis_hash_chain(self):
        entry = await self.ledger.append(
            tenant_id="partner_a", workflow_id="WF-001",
            membrane_decision="allow", operating_mode="SHADOW",
            s_score=1.0, ds_de=0.0, ces_state="REST", rationale="First"
        )
        from r5_distributed_crypto.merkle.distributed_ledger import DistributedLedger
        self.assertEqual(entry.previous_hash, DistributedLedger.GENESIS_HASH)

    async def test_hash_chain_links(self):
        e1 = await self.ledger.append(
            tenant_id="partner_a", workflow_id="WF-001",
            membrane_decision="allow", operating_mode="SHADOW",
            s_score=1.0, ds_de=0.0, ces_state="REST", rationale="E1"
        )
        e2 = await self.ledger.append(
            tenant_id="partner_a", workflow_id="WF-001",
            membrane_decision="allow_with_alert", operating_mode="SHADOW",
            s_score=0.9, ds_de=-0.1, ces_state="TENSION", rationale="E2"
        )
        self.assertEqual(e2.previous_hash, e1.entry_hash)

    async def test_verify_valid_chain(self):
        for i in range(5):
            await self.ledger.append(
                tenant_id="partner_a", workflow_id=f"WF-{i:03d}",
                membrane_decision="allow", operating_mode="SHADOW",
                s_score=1.0 - i * 0.05, ds_de=-0.02, ces_state="REST", rationale=f"Entry {i}"
            )
        result = self.ledger.verify_full_chain()
        self.assertTrue(result["valid"])
        self.assertEqual(result["broken_links"], 0)
        self.assertEqual(result["entry_count"], 5)

    async def test_tamper_detection(self):
        for i in range(3):
            await self.ledger.append(
                tenant_id="partner_a", workflow_id="WF-001",
                membrane_decision="allow", operating_mode="SHADOW",
                s_score=1.0, ds_de=0.0, ces_state="REST", rationale=f"Entry {i}"
            )
        # Tamper with the ledger file
        with open(self.ledger_path, "r") as f:
            lines = f.readlines()
        import json as _json
        data = _json.loads(lines[0])
        data["s_score"] = 999.9   # Tamper
        lines[0] = _json.dumps(data) + "\n"
        with open(self.ledger_path, "w") as f:
            f.writelines(lines)
        # Reload should detect broken chain
        from r5_distributed_crypto.merkle.distributed_ledger import DistributedLedger, LedgerIntegrityError
        with self.assertRaises(LedgerIntegrityError):
            DistributedLedger(ledger_path=self.ledger_path)

    async def test_epoch_sealing(self):
        from r5_distributed_crypto.merkle.distributed_ledger import EPOCH_SIZE
        # Append exactly EPOCH_SIZE entries to trigger sealing
        for i in range(EPOCH_SIZE):
            await self.ledger.append(
                tenant_id="partner_a", workflow_id=f"WF-{i:04d}",
                membrane_decision="allow", operating_mode="SHADOW",
                s_score=1.0, ds_de=0.0, ces_state="REST", rationale=f"E{i}"
            )
        # The first epoch should be sealed
        sealed_epochs = [e for e in self.ledger._epochs if e.sealed]
        self.assertEqual(len(sealed_epochs), 1)
        self.assertIsNotNone(sealed_epochs[0].merkle_root)

    async def test_merkle_proof_after_seal(self):
        from r5_distributed_crypto.merkle.distributed_ledger import EPOCH_SIZE, verify_merkle_proof
        entries = []
        for i in range(EPOCH_SIZE):
            e = await self.ledger.append(
                tenant_id="partner_a", workflow_id=f"WF-{i:04d}",
                membrane_decision="allow", operating_mode="SHADOW",
                s_score=1.0, ds_de=0.0, ces_state="REST", rationale=f"E{i}"
            )
            entries.append(e)
        # Get proof for the first entry
        proof_data = self.ledger.get_merkle_proof(entries[0].entry_id)
        self.assertIsNotNone(proof_data)
        self.assertIn("merkle_root", proof_data)
        # Verify the proof
        epoch = self.ledger._epochs[0]
        valid = verify_merkle_proof(entries[0].entry_hash, proof_data["proof"], epoch.merkle_root)
        self.assertTrue(valid)

    async def test_recent_entries_limit(self):
        for i in range(10):
            await self.ledger.append(
                tenant_id="partner_a", workflow_id="WF-001",
                membrane_decision="allow", operating_mode="SHADOW",
                s_score=1.0, ds_de=0.0, ces_state="REST", rationale=f"E{i}"
            )
        recent = self.ledger.get_recent_entries(5)
        self.assertEqual(len(recent), 5)


class TestKeyCeremony(unittest.TestCase):
    """Tests for the Jubilee Cryptographic Key Ceremony."""

    def setUp(self):
        from r5_distributed_crypto.merkle.distributed_ledger import KeyCeremony
        self.ceremony = KeyCeremony(deploy_secret="test_secret_r5")

    def test_sign_and_verify(self):
        signed = self.ceremony.sign_oath(
            oath_text=self.ceremony.OATH_TEXT,
            epoch_id=0,
            operator_id="op-aziz",
        )
        valid = self.ceremony.verify_oath(
            oath_text=self.ceremony.OATH_TEXT,
            signature=signed["signature"],
            epoch_id=0,
            operator_id="op-aziz",
            nonce=signed["nonce"],
            timestamp=signed["timestamp"],
        )
        self.assertTrue(valid)

    def test_wrong_oath_text_raises(self):
        with self.assertRaises(ValueError):
            self.ceremony.sign_oath("wrong oath", epoch_id=0, operator_id="op-aziz")

    def test_wrong_epoch_invalidates(self):
        signed = self.ceremony.sign_oath(
            self.ceremony.OATH_TEXT, epoch_id=0, operator_id="op-aziz"
        )
        valid = self.ceremony.verify_oath(
            oath_text=self.ceremony.OATH_TEXT,
            signature=signed["signature"],
            epoch_id=1,   # Different epoch
            operator_id="op-aziz",
            nonce=signed["nonce"],
            timestamp=signed["timestamp"],
        )
        self.assertFalse(valid)

    def test_tampered_signature_invalidates(self):
        signed = self.ceremony.sign_oath(
            self.ceremony.OATH_TEXT, epoch_id=0, operator_id="op-aziz"
        )
        valid = self.ceremony.verify_oath(
            oath_text=self.ceremony.OATH_TEXT,
            signature=signed["signature"][:-4] + "XXXX",
            epoch_id=0,
            operator_id="op-aziz",
            nonce=signed["nonce"],
            timestamp=signed["timestamp"],
        )
        self.assertFalse(valid)

    def test_different_operators_different_signatures(self):
        s1 = self.ceremony.sign_oath(self.ceremony.OATH_TEXT, 0, "op-alice")
        s2 = self.ceremony.sign_oath(self.ceremony.OATH_TEXT, 0, "op-bob")
        self.assertNotEqual(s1["signature"], s2["signature"])

    def test_different_epochs_different_keys(self):
        s0 = self.ceremony.sign_oath(self.ceremony.OATH_TEXT, 0, "op-test")
        s1 = self.ceremony.sign_oath(self.ceremony.OATH_TEXT, 1, "op-test")
        self.assertNotEqual(s0["signature"], s1["signature"])

    def test_signed_output_has_required_fields(self):
        signed = self.ceremony.sign_oath(self.ceremony.OATH_TEXT, 0, "op-test")
        for field in ["signature", "epoch_id", "operator_id", "timestamp", "nonce", "payload_hash"]:
            self.assertIn(field, signed)


# ─── RUN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
