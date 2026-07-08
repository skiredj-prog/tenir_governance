from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tenir_v4_test import (
    PilotValidationError,
    build_pilot_payload,
    evidence_gate_for,
    normalize_raw_score,
    sample_pilot_template,
    validate_pilot_payload,
)


ROOT = Path(__file__).resolve().parents[1]


class AdjudicationScoringTests(unittest.TestCase):
    def test_normalize_raw_score_uses_zero_to_hundred_scale(self) -> None:
        self.assertEqual(normalize_raw_score(0), 0)
        self.assertEqual(normalize_raw_score(2.5), 50)
        self.assertEqual(normalize_raw_score(5), 100)

    def test_evidence_gate_matches_thresholds(self) -> None:
        self.assertEqual(evidence_gate_for(49), "LEARN_ONLY")
        self.assertEqual(evidence_gate_for(50), "HOLD_OR_TRANSMIT")
        self.assertEqual(evidence_gate_for(69), "HOLD_OR_TRANSMIT")
        self.assertEqual(evidence_gate_for(70), "FULL_ACTION")

    def test_amplified_loop_reaches_enforce_only_with_full_gate(self) -> None:
        payload = build_pilot_payload(sample_pilot_template("amplified_loop"))
        self.assertEqual(payload["adjudication"]["verdict"], "REJECT")
        self.assertEqual(payload["adjudication"]["next_action"], "ENFORCE")
        self.assertEqual(payload["scores"]["evidence_gate"], "FULL_ACTION")
        self.assertGreaterEqual(payload["scores"]["family_totals"]["evidence_coverage_score"], 70)

    def test_authority_contradiction_routes_to_transmit(self) -> None:
        payload = build_pilot_payload(sample_pilot_template("authority_contradiction"))
        self.assertEqual(payload["adjudication"]["authority_status"], "CONTRADICTED")
        self.assertEqual(payload["adjudication"]["next_action"], "TRANSMIT")

    def test_low_evidence_caps_action_to_learn(self) -> None:
        template = sample_pilot_template("stabilizing_loop")
        template["scores"]["raw_dimensions"]["evidence_coverage"] = {
            "provenance": 1.5,
            "timeliness": 1.6,
            "completeness": 1.2,
            "cross_signal_consistency": 1.4,
        }
        payload = build_pilot_payload(template)
        self.assertEqual(payload["scores"]["evidence_gate"], "LEARN_ONLY")
        self.assertEqual(payload["adjudication"]["next_action"], "LEARN")

    def test_payload_validation_rejects_missing_loop_stage(self) -> None:
        payload = build_pilot_payload(sample_pilot_template("stabilizing_loop"))
        del payload["loop"]["feedback_return"]
        with self.assertRaises(PilotValidationError):
            validate_pilot_payload(payload)


class AdjudicationToolingTests(unittest.TestCase):
    def test_generation_tool_writes_valid_payload(self) -> None:
        tool = ROOT / "tools" / "generate_ocp_sovereignty_pilot.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "pilot.json"
            subprocess.run(
                [
                    sys.executable,
                    str(tool),
                    "--scenario",
                    "stabilizing_loop",
                    "--output-json",
                    str(output),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            validate_pilot_payload(payload)
            self.assertEqual(payload["pilot"]["reporting_mode"], "dual")


if __name__ == "__main__":
    unittest.main()
