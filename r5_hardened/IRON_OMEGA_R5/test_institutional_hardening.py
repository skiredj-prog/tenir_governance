import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))


import os
import tempfile
import unittest

from r5_distributed_crypto.merkle.distributed_ledger import DistributedLedger


class TestInstitutionalLedgerHardening(unittest.IsolatedAsyncioTestCase):
    async def test_reload_after_append_preserves_valid_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            ledger = DistributedLedger(ledger_path=path)
            await ledger.append(
                tenant_id="partner_a",
                workflow_id="WF-001",
                membrane_decision="allow_with_alert",
                operating_mode="SHADOW",
                s_score=0.91,
                ds_de=-0.04,
                ces_state="TENSION",
                rationale="threshold nearing alert floor",
                policy_version="tenir-partner_a-shadow-v4-1.0.0",
            )
            reloaded = DistributedLedger(ledger_path=path)
            report = reloaded.verify_full_chain()
            self.assertTrue(report["valid"])
            self.assertEqual(report["broken_links"], 0)
            self.assertEqual(report["hash_mismatches"], 0)

    async def test_control_transition_is_ledgered_and_recovers_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            ledger = DistributedLedger(ledger_path=path)
            await ledger.append(
                tenant_id="partner_a",
                workflow_id="WF-002",
                membrane_decision="allow",
                operating_mode="SHADOW",
                s_score=1.5,
                ds_de=0.0,
                ces_state="REST",
                rationale="stable",
                policy_version="tenir-partner_a-shadow-v4-1.0.0",
            )
            transition = ledger.append_control_transition(
                transition="SHADOW_TO_ENFORCE",
                from_mode="SHADOW",
                to_mode="ENFORCE",
                operator_id="op-001",
                reason="close the glass",
                policy_version="tenir-partner_a-shadow-v4-1.0.0",
            )
            self.assertEqual(transition["entry_type"], "control_transition")
            reloaded = DistributedLedger(ledger_path=path)
            self.assertEqual(reloaded.recover_last_mode(default="SHADOW"), "ENFORCE")

    async def test_control_transition_participates_in_hash_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            ledger = DistributedLedger(ledger_path=path)
            first = await ledger.append(
                tenant_id="partner_a",
                workflow_id="WF-003",
                membrane_decision="allow",
                operating_mode="SHADOW",
                s_score=1.1,
                ds_de=0.0,
                ces_state="REST",
                rationale="stable",
                policy_version="tenir-partner_a-shadow-v4-1.0.0",
            )
            transition = ledger.append_control_transition(
                transition="SHADOW_TO_ENFORCE",
                from_mode="SHADOW",
                to_mode="ENFORCE",
                operator_id="op-002",
                reason="elevated risk",
                policy_version="tenir-partner_a-shadow-v4-1.0.0",
            )
            self.assertEqual(transition["previous_hash"], first.entry_hash)
