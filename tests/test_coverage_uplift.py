"""
tests/test_coverage_uplift.py
=================================
Coverage uplift suite — brings overall tenir_governance coverage from 72% to 85-90%.

Targets (by module):
  sdk.py           40% → ~88%   (TENIRGovernanceClient, GovernanceEvent, GovernanceResult)
  validator.py     64% → ~88%   (validate_event_sample, validate_ledger_chain, CLI main)
  copy_lint.py     62% → ~88%   (CopyLinter.lint_file/paths, detect_exposure, CLI main)
  ledger_migrate.py 69% → ~88%  (migrate_ledger, verify_migrated_ledger, CLI main)
  regression_corpus 83% → 100%  (get_case, scenario_group, tagged, nsl_cases)
"""

from __future__ import annotations

import json
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest

# ─── SDK ─────────────────────────────────────────────────────────────────────

from tenir_governance.sdk import TENIRGovernanceClient, GovernanceEvent, GovernanceResult
from tenir_governance.policy_engine import PolicyEngine, PolicyViolation
from tenir_governance.nomenclature import (
    OperatingModeNames, MembraneDecisionNames, CESStateNames,
)


class TestGovernanceEvent:
    def test_default_fields(self):
        ev = GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0)
        assert ev.workflow_type == "rnd"
        assert ev.actor_ref == "operator"
        assert ev.option_space is None
        assert ev.nsl_input is None

    def test_custom_fields(self):
        ev = GovernanceEvent(
            pressure=0.8, velocity=0.6, capacity=1.2,
            option_space=0.7, workflow_id="WF-001",
            nsl_input="HOLD p=0.8", nsl_backend="grammar", nsl_confidence=0.95,
        )
        assert ev.workflow_id == "WF-001"
        assert ev.nsl_backend == "grammar"
        assert ev.nsl_confidence == 0.95


class TestGovernanceResult:
    """Test GovernanceResult properties and serialisation methods."""

    def _make_result(self, ces_state=CESStateNames.REST,
                     decision=MembraneDecisionNames.ALLOW) -> GovernanceResult:
        return GovernanceResult(
            event_id="ev-001",
            observed_at="2026-01-01T00:00:00Z",
            s_score=2.5,
            ds_de=0.1,
            d2s_de2=0.0,
            projected_events_to_zero=None,
            decision=decision,
            rationale="All clear",
            alert=False,
            intended_block=False,
            ces_state=ces_state,
            operating_mode=OperatingModeNames.SHADOW_PASSIVE,
            policy_version="tenir-canonical-v1.0.0",
            chain_hash="abc123def456789012345678901234567890abcdef",
            entry_index=1,
        )

    def test_business_label_rest(self):
        r = self._make_result(ces_state=CESStateNames.REST)
        label = r.business_label
        assert isinstance(label, str) and len(label) > 0

    def test_business_label_tension(self):
        r = self._make_result(ces_state=CESStateNames.TENSION)
        assert isinstance(r.business_label, str)

    def test_business_label_collapse(self):
        r = self._make_result(ces_state=CESStateNames.COLLAPSE)
        assert isinstance(r.business_label, str)

    def test_business_label_signal_conflict(self):
        r = self._make_result(ces_state=CESStateNames.SIGNAL_CONFLICT)
        assert isinstance(r.business_label, str)

    def test_decision_label(self):
        r = self._make_result()
        assert isinstance(r.decision_label, str)

    def test_is_blocked_false(self):
        r = self._make_result(decision=MembraneDecisionNames.ALLOW)
        assert r.is_blocked is False

    def test_is_blocked_true(self):
        r = self._make_result(decision=MembraneDecisionNames.BLOCK)
        assert r.is_blocked is True

    def test_recommended_posture(self):
        r = self._make_result()
        assert isinstance(r.recommended_posture, str)

    def test_to_business_payload_structure(self):
        r = self._make_result()
        p = r.to_business_payload()
        assert "stability_index" in p
        assert "institutional_state" in p
        assert "governance_action" in p
        assert "chain_hash" in p
        assert p["chain_hash"].endswith("…")  # truncated with ellipsis
        assert "entry_number" in p

    def test_to_r5_frame_structure(self):
        r = self._make_result()
        f = r.to_r5_frame()
        assert "s_score" in f
        assert "ces_state" in f
        assert "membrane_decision" in f
        assert "operating_mode" in f


class TestTENIRGovernanceClient:
    """Full adjudication pipeline tests."""

    def test_client_instantiates_with_default_policy(self):
        client = TENIRGovernanceClient()
        assert client.policy is not None
        assert client.operating_mode == OperatingModeNames.SHADOW_PASSIVE

    def test_for_demo_classmethod(self):
        client = TENIRGovernanceClient.for_demo()
        assert isinstance(client.policy, PolicyEngine)

    def test_adjudicate_single_event(self):
        client = TENIRGovernanceClient()
        ev = GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0)
        result = client.adjudicate(ev)
        assert isinstance(result, GovernanceResult)
        assert result.s_score > 0
        assert result.ces_state in CESStateNames.ALL
        _valid_decisions = {MembraneDecisionNames.ALLOW, MembraneDecisionNames.ALLOW_WITH_ALERT,
                            MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK, MembraneDecisionNames.BLOCK}
        assert result.decision in _valid_decisions
        assert result.entry_index == 1

    def test_adjudicate_accumulates_trajectory_derivatives(self):
        client = TENIRGovernanceClient()
        # Feed three events so d2s_de2 becomes non-zero
        for p in [0.5, 0.6, 0.7]:
            ev = GovernanceEvent(pressure=p, velocity=0.5, capacity=1.0)
            result = client.adjudicate(ev)
        # By third event, derivatives should be computed
        assert isinstance(result.ds_de, float)
        assert isinstance(result.d2s_de2, float)

    def test_adjudicate_with_negative_ds_computes_horizon(self):
        """Decreasing S → projected events to zero should be computed."""
        client = TENIRGovernanceClient()
        ev1 = GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0)
        ev2 = GovernanceEvent(pressure=2.0, velocity=2.0, capacity=0.5)
        client.adjudicate(ev1)
        result = client.adjudicate(ev2)
        # ds_de may be negative, horizon may be int or None
        assert result.projected_events_to_zero is None or isinstance(result.projected_events_to_zero, int)

    def test_adjudicate_with_option_space(self):
        client = TENIRGovernanceClient()
        ev = GovernanceEvent(pressure=0.8, velocity=0.8, capacity=0.3, option_space=0.2)
        result = client.adjudicate(ev)
        _valid_decisions = {MembraneDecisionNames.ALLOW, MembraneDecisionNames.ALLOW_WITH_ALERT,
                            MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK, MembraneDecisionNames.BLOCK}
        assert result.decision in _valid_decisions

    def test_adjudicate_uses_workflow_id(self):
        client = TENIRGovernanceClient()
        ev = GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0, workflow_id="MY-WF")
        result = client.adjudicate(ev)
        assert result.event_id == "MY-WF"

    def test_adjudicate_auto_generates_event_id_when_missing(self):
        client = TENIRGovernanceClient()
        ev = GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0)
        result = client.adjudicate(ev)
        assert len(result.event_id) > 0

    def test_adjudicate_from_nsl(self):
        client = TENIRGovernanceClient()
        nsl_result = {
            "intent": "HOLD",
            "params": {"pressure": 0.7, "velocity": 0.6, "capacity": 0.9, "option_space": 0.5},
            "entity_type": "rnd",
            "raw_input": "HOLD p=0.7",
            "backend": "grammar",
            "confidence": 0.88,
        }
        result = client.adjudicate_from_nsl(nsl_result)
        assert isinstance(result, GovernanceResult)

    def test_adjudicate_from_nsl_minimal(self):
        """nsl_result with missing params falls back to defaults."""
        client = TENIRGovernanceClient()
        result = client.adjudicate_from_nsl({"intent": "PROBE", "params": {}})
        assert isinstance(result, GovernanceResult)

    def test_transition_mode(self):
        client = TENIRGovernanceClient()
        old_mode = client.operating_mode
        client.transition_mode(OperatingModeNames.ENFORCE, "op-1", "pilot escalation")
        assert client.operating_mode == OperatingModeNames.ENFORCE
        # Ledger should have a CONTROL_TRANSITION entry
        assert any(
            e["payload"].get("type") == "control_transition"
            for e in client._in_memory_log
        )

    def test_current_state_before_any_event(self):
        client = TENIRGovernanceClient()
        state = client.current_state()
        assert state["mode"] == OperatingModeNames.SHADOW_PASSIVE
        assert state["entry_count"] == 0
        assert state["current_s"] is None

    def test_current_state_after_event(self):
        client = TENIRGovernanceClient()
        client.adjudicate(GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0))
        state = client.current_state()
        assert state["entry_count"] == 1
        assert state["current_s"] is not None
        assert "policy_version" in state
        assert "policy_fingerprint" in state

    def test_validate_chain_empty(self):
        client = TENIRGovernanceClient()
        assert client.validate_chain() is True

    def test_validate_chain_after_adjudications(self):
        client = TENIRGovernanceClient()
        for i in range(5):
            client.adjudicate(GovernanceEvent(pressure=0.1*i, velocity=0.5, capacity=1.0))
        assert client.validate_chain() is True

    def test_ledger_written_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "test_ledger.jsonl"
            client = TENIRGovernanceClient(ledger_path=ledger_path)
            client.adjudicate(GovernanceEvent(pressure=0.5, velocity=0.5, capacity=1.0))
            assert ledger_path.exists()
            lines = ledger_path.read_text().strip().splitlines()
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert "chain_hash" in entry
            assert "payload" in entry

    def test_ledger_file_grows_with_multiple_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "multi.jsonl"
            client = TENIRGovernanceClient(ledger_path=ledger_path)
            for i in range(3):
                client.adjudicate(GovernanceEvent(pressure=0.3 + i*0.2, velocity=0.5, capacity=1.0))
            lines = ledger_path.read_text().strip().splitlines()
            assert len(lines) == 3

    def test_enforce_mode_blocks_tau_breach(self):
        client = TENIRGovernanceClient(
            operating_mode=OperatingModeNames.ENFORCE,
            validate_on_init=True,
        )
        # Very high pressure+velocity, low capacity → S well below tau
        ev = GovernanceEvent(pressure=5.0, velocity=5.0, capacity=0.1)
        result = client.adjudicate(ev)
        assert result.decision == MembraneDecisionNames.BLOCK

    def test_shadow_mode_intended_block_not_hard_block(self):
        client = TENIRGovernanceClient(
            operating_mode=OperatingModeNames.SHADOW_PASSIVE,
        )
        ev = GovernanceEvent(pressure=5.0, velocity=5.0, capacity=0.1)
        result = client.adjudicate(ev)
        assert result.decision != MembraneDecisionNames.BLOCK
        assert result.intended_block is True


class TestSDKCLI:
    """Cover the main() CLI entry point in sdk.py."""

    def test_cli_with_inline_args(self, capsys, monkeypatch):
        from tenir_governance.sdk import main
        monkeypatch.setattr(sys, "argv", ["tenir-sdk", "--pressure", "0.5", "--velocity", "0.5", "--capacity", "1.0"])
        main()
        out = capsys.readouterr().out
        assert len(out) > 0

    def test_cli_business_flag(self, capsys, monkeypatch):
        from tenir_governance.sdk import main
        monkeypatch.setattr(sys, "argv", ["tenir-sdk", "--pressure", "0.5", "--velocity", "0.5", "--capacity", "1.0", "--business"])
        main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "stability_index" in data

    def test_cli_events_file(self, capsys, tmp_path, monkeypatch):
        from tenir_governance.sdk import main
        events = [
            {"pressure": 0.4, "velocity": 0.4, "capacity": 1.0},
            {"pressure": 0.8, "velocity": 0.7, "capacity": 0.5},
        ]
        events_file = tmp_path / "events.json"
        events_file.write_text(json.dumps(events), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["tenir-sdk", "--events", str(events_file)])
        main()
        out = capsys.readouterr().out
        lines = [l for l in out.strip().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_cli_events_file_business(self, capsys, tmp_path, monkeypatch):
        from tenir_governance.sdk import main
        events = [{"pressure": 0.4, "velocity": 0.4, "capacity": 1.0}]
        events_file = tmp_path / "events.json"
        events_file.write_text(json.dumps(events), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["tenir-sdk", "--events", str(events_file), "--business"])
        main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "institutional_state" in data


# ─── VALIDATOR ───────────────────────────────────────────────────────────────

from tenir_governance.validator import TENIRValidator, ValidationReport, ValidationFinding


class TestValidationReport:
    def test_summary_passes(self):
        report = ValidationReport()
        report.findings.append(ValidationFinding("PASS", "T-001", "All good"))
        s = report.summary()
        assert "PASSED" in s
        assert "T-001" in s

    def test_summary_fails(self):
        report = ValidationReport()
        report.findings.append(ValidationFinding("FAIL", "T-001", "Something broke", "detail here"))
        s = report.summary()
        assert "FAILED" in s

    def test_summary_warn(self):
        report = ValidationReport()
        report.findings.append(ValidationFinding("WARN", "T-002", "Advisory", None))
        s = report.summary()
        assert "⚠" in s or "PASSED" in s  # warn doesn't fail

    def test_to_dict_structure(self):
        report = ValidationReport()
        report.findings.append(ValidationFinding("PASS", "P-001", "ok", "detail"))
        report.findings.append(ValidationFinding("FAIL", "F-001", "bad"))
        d = report.to_dict()
        assert d["passed"] is False
        assert d["fail_count"] == 1
        assert d["warn_count"] == 0
        assert len(d["findings"]) == 2


class TestTENIRValidator:
    def test_for_ci_factory(self):
        v = TENIRValidator.for_ci()
        assert v.expected_version == "tenir-canonical-v1.0.0"

    def test_validate_all_passes(self):
        v = TENIRValidator()
        report = v.validate_all()
        assert report.passed, report.summary()

    def test_expected_version_mismatch_fails(self):
        v = TENIRValidator(expected_version="wrong-version-0.0.0")
        report = v.validate_all()
        assert not report.passed
        fail_ids = [f.check_id for f in report.findings if f.level == "FAIL"]
        assert "POL-002" in fail_ids

    def test_validate_event_sample_valid(self):
        v = TENIRValidator()
        report = v.validate_event_sample({"pressure": 0.5, "velocity": 0.5, "capacity": 1.0})
        assert report.passed

    def test_validate_event_sample_with_option_space(self):
        v = TENIRValidator()
        report = v.validate_event_sample({
            "pressure": 0.5, "velocity": 0.5, "capacity": 1.0, "option_space": 0.7
        })
        assert report.passed

    def test_validate_event_sample_missing_field(self):
        v = TENIRValidator()
        report = v.validate_event_sample({"pressure": 0.5, "velocity": 0.5})  # no capacity
        assert not report.passed
        assert any("capacity" in (f.description + (f.detail or "")) for f in report.findings if f.level == "FAIL")

    def test_validate_event_sample_negative_value(self):
        v = TENIRValidator()
        report = v.validate_event_sample({"pressure": -0.1, "velocity": 0.5, "capacity": 1.0})
        assert not report.passed

    def test_validate_event_sample_non_numeric(self):
        v = TENIRValidator()
        report = v.validate_event_sample({"pressure": "high", "velocity": 0.5, "capacity": 1.0})
        assert not report.passed

    def test_validate_event_sample_option_space_out_of_range(self):
        v = TENIRValidator()
        report = v.validate_event_sample({
            "pressure": 0.5, "velocity": 0.5, "capacity": 1.0, "option_space": 1.5
        })
        assert not report.passed

    def test_validate_event_sample_non_numeric_option_space(self):
        v = TENIRValidator()
        report = v.validate_event_sample({
            "pressure": 0.5, "velocity": 0.5, "capacity": 1.0, "option_space": "medium"
        })
        assert not report.passed

    def test_validate_ledger_chain_valid(self, tmp_path):
        """Build a valid chain manually and verify it passes."""
        import hashlib, json as _json
        ledger_file = tmp_path / "chain.jsonl"
        prev = "GENESIS"
        with ledger_file.open("w") as f:
            for i in range(3):
                payload = {"type": "observation", "index": i, "s_score": 1.5 - i * 0.1}
                clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
                chain_hash = hashlib.sha256(f"{prev}|{clean}".encode()).hexdigest()
                entry = {"previous_hash": prev, "chain_hash": chain_hash,
                         "timestamp": "2026-01-01T00:00:00Z", "payload": payload}
                f.write(_json.dumps(entry) + "\n")
                prev = chain_hash

        v = TENIRValidator()
        report = v.validate_ledger_chain(ledger_file)
        assert report.passed, report.summary()

    def test_validate_ledger_chain_nonexistent_file(self, tmp_path):
        v = TENIRValidator()
        report = v.validate_ledger_chain(tmp_path / "nonexistent.jsonl")
        # Should warn, not fail
        assert report.passed
        assert any(f.level == "WARN" for f in report.findings)

    def test_validate_ledger_chain_empty_file(self, tmp_path):
        ledger_file = tmp_path / "empty.jsonl"
        ledger_file.write_text("", encoding="utf-8")
        v = TENIRValidator()
        report = v.validate_ledger_chain(ledger_file)
        assert any(f.level == "WARN" for f in report.findings)

    def test_validate_ledger_chain_broken(self, tmp_path):
        """Tampered previous_hash breaks the chain."""
        import hashlib, json as _json
        ledger_file = tmp_path / "broken.jsonl"
        payload = {"type": "obs", "index": 0}
        clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        chain_hash = hashlib.sha256(f"GENESIS|{clean}".encode()).hexdigest()
        # Corrupt the previous_hash on second entry
        entry1 = {"previous_hash": "GENESIS", "chain_hash": chain_hash,
                  "timestamp": "T", "payload": payload}
        entry2 = {"previous_hash": "TAMPERED_HASH", "chain_hash": "xxx",
                  "timestamp": "T", "payload": {"index": 1}}
        with ledger_file.open("w") as f:
            f.write(json.dumps(entry1) + "\n")
            f.write(json.dumps(entry2) + "\n")
        v = TENIRValidator()
        report = v.validate_ledger_chain(ledger_file)
        assert not report.passed

    def test_validate_ledger_chain_invalid_json(self, tmp_path):
        ledger_file = tmp_path / "bad.jsonl"
        ledger_file.write_text("{not json}\n", encoding="utf-8")
        v = TENIRValidator()
        report = v.validate_ledger_chain(ledger_file)
        assert not report.passed


class TestValidatorCLI:
    def test_cli_default_passes(self, capsys):
        from tenir_governance.validator import main
        rc = main(["--policy", "default"])
        assert rc == 0

    def test_cli_json_output(self, capsys):
        from tenir_governance.validator import main
        rc = main(["--policy", "default", "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["passed"] is True
        assert rc == 0

    def test_cli_expected_version_mismatch(self, capsys):
        from tenir_governance.validator import main
        rc = main(["--expected-version", "wrong-v99"])
        assert rc == 1

    def test_cli_with_valid_ledger(self, capsys, tmp_path):
        import hashlib, json as _json
        from tenir_governance.validator import main
        ledger_file = tmp_path / "valid.jsonl"
        payload = {"type": "obs", "index": 0}
        clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        chain_hash = hashlib.sha256(f"GENESIS|{clean}".encode()).hexdigest()
        entry = {"previous_hash": "GENESIS", "chain_hash": chain_hash,
                 "timestamp": "2026-01-01T00:00:00Z", "payload": payload}
        ledger_file.write_text(_json.dumps(entry) + "\n", encoding="utf-8")
        rc = main(["--ledger", str(ledger_file)])
        assert rc == 0

    def test_cli_json_with_valid_ledger(self, capsys, tmp_path):
        import hashlib, json as _json
        from tenir_governance.validator import main
        ledger_file = tmp_path / "v2.jsonl"
        payload = {"type": "obs"}
        clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        h = hashlib.sha256(f"GENESIS|{clean}".encode()).hexdigest()
        ledger_file.write_text(_json.dumps({"previous_hash": "GENESIS", "chain_hash": h,
                                             "payload": payload}) + "\n")
        rc = main(["--ledger", str(ledger_file), "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "passed" in data
        assert rc == 0


# ─── COPY-LINT ────────────────────────────────────────────────────────────────

from tenir_governance.copy_lint import CopyLinter, LintReport, detect_exposure


class TestDetectExposure:
    def test_detects_public_yaml_front_matter(self):
        content = "---\nexposure: public\n---\n\nSome content."
        assert detect_exposure(content) == "public"

    def test_detects_operator_exposure(self):
        content = "---\nexposure: operator\n---\nContent."
        assert detect_exposure(content) == "operator"

    def test_default_exposure_when_no_front_matter(self):
        content = "Just some content without front matter."
        assert detect_exposure(content) == "operator"

    def test_custom_default(self):
        content = "No front matter here."
        assert detect_exposure(content, default="canonical") == "canonical"


class TestLintReport:
    def test_summary_clean(self):
        report = LintReport()
        s = report.summary()
        assert "CLEAN" in s

    def test_summary_with_blocking_finding(self, tmp_path):
        from tenir_governance.copy_lint import Finding
        report = LintReport()
        report.findings.append(Finding(
            file="doc.md", line=5, column=1, term="canonical",
            category="banned", severity="BLOCK",
            message="Banned term 'canonical'", exposure="public",
            snippet="This is a canonical document.",
        ))
        s = report.summary()
        assert "BLOCKING" in s
        assert "doc.md" in s

    def test_to_dict(self):
        from tenir_governance.copy_lint import Finding
        report = LintReport()
        report.findings.append(Finding(
            file="x.md", line=1, column=1, term="test",
            category="banned", severity="WARN",
            message="msg", exposure="operator",
        ))
        d = report.to_dict()
        assert d["blocking"] is False
        assert len(d["findings"]) == 1


class TestCopyLinter:
    def test_clean_file_returns_no_findings(self, tmp_path):
        doc = tmp_path / "clean.md"
        doc.write_text("This document talks about governance software.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_file(doc)
        assert len(report.findings) == 0

    def test_banned_term_in_operator_file_produces_warn(self, tmp_path):
        doc = tmp_path / "op.md"
        doc.write_text("This is our canonical process document.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_file(doc)
        findings = [f for f in report.findings if f.category == "banned"]
        assert len(findings) >= 1
        assert all(f.severity == "WARN" for f in findings)

    def test_banned_term_in_public_file_produces_block(self, tmp_path):
        doc = tmp_path / "pub.md"
        doc.write_text("---\nexposure: public\n---\nThis is canonical.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_file(doc)
        findings = [f for f in report.findings if f.category == "banned"]
        assert any(f.severity == "BLOCK" for f in findings)

    def test_strict_mode_makes_all_banned_blocking(self, tmp_path):
        doc = tmp_path / "strict.md"
        doc.write_text("The canonical approach.\n", encoding="utf-8")
        linter = CopyLinter(strict=True)
        report = linter.lint_file(doc)
        assert report.blocking

    def test_override_exposure_to_public(self, tmp_path):
        doc = tmp_path / "override.md"
        doc.write_text("The canonical approach.\n", encoding="utf-8")  # no front matter
        linter = CopyLinter(override_exposure="public")
        report = linter.lint_file(doc)
        findings = [f for f in report.findings if f.category == "banned"]
        assert any(f.severity == "BLOCK" for f in findings)

    def test_deprecated_cave_expansion_produces_block(self, tmp_path):
        doc = tmp_path / "cave.md"
        doc.write_text("CAVE stands for Control · Aim · Veto · Epistemics.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_file(doc)
        findings = [f for f in report.findings if f.category == "deprecated_cave"]
        assert any(f.severity == "BLOCK" for f in findings)

    def test_unreadable_file_returns_block_finding(self, tmp_path):
        """Simulate OS error by pointing at a directory path as a file."""
        # Use a path that simply doesn't exist to trigger OSError on read
        missing = tmp_path / "ghost.md"
        linter = CopyLinter()
        report = linter.lint_file(missing)  # file does not exist
        assert any(f.category == "io_error" for f in report.findings)

    def test_lint_paths_with_directory(self, tmp_path):
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "a.md").write_text("Clean content.\n", encoding="utf-8")
        (sub / "b.md").write_text("The canonical membrane.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_paths([sub])
        assert isinstance(report, LintReport)

    def test_lint_paths_with_mixed_list(self, tmp_path):
        doc1 = tmp_path / "one.md"
        doc1.write_text("Clean.\n", encoding="utf-8")
        doc2 = tmp_path / "two.txt"
        doc2.write_text("The schizophrenia of the system.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_paths([doc1, doc2])
        terms = [f.term for f in report.findings]
        assert any("schizophrenia" in t.lower() for t in terms)

    def test_untranslated_branded_term_in_public_file(self, tmp_path):
        doc = tmp_path / "pub_brand.md"
        # TAU appears but without any translation hint nearby
        doc.write_text("---\nexposure: public\n---\nWe use TAU here.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_file(doc)
        # Should get an untranslated warning
        untranslated = [f for f in report.findings if f.category == "untranslated"]
        assert len(untranslated) >= 1

    def test_translated_branded_term_no_finding(self, tmp_path):
        doc = tmp_path / "pub_trans.md"
        doc.write_text("---\nexposure: public\n---\nTAU — the governance score.\n", encoding="utf-8")
        linter = CopyLinter()
        report = linter.lint_file(doc)
        untranslated = [f for f in report.findings if f.category == "untranslated"]
        assert len(untranslated) == 0


class TestCopyLintCLI:
    def test_cli_clean_file_exits_0(self, capsys, tmp_path):
        from tenir_governance.copy_lint import main
        doc = tmp_path / "clean.md"
        doc.write_text("All good here.\n", encoding="utf-8")
        rc = main([str(doc)])
        assert rc == 0

    def test_cli_banned_operator_file_exits_0(self, capsys, tmp_path):
        """Operator-exposure banned term is WARN, not BLOCK → still exits 0."""
        from tenir_governance.copy_lint import main
        doc = tmp_path / "op.md"
        doc.write_text("Our canonical approach.\n", encoding="utf-8")
        rc = main([str(doc)])
        assert rc == 0

    def test_cli_banned_public_file_exits_1(self, capsys, tmp_path):
        from tenir_governance.copy_lint import main
        doc = tmp_path / "pub.md"
        doc.write_text("---\nexposure: public\n---\ncanonical.\n", encoding="utf-8")
        rc = main([str(doc)])
        assert rc == 1

    def test_cli_json_output(self, capsys, tmp_path):
        from tenir_governance.copy_lint import main
        doc = tmp_path / "clean.md"
        doc.write_text("Clean.\n", encoding="utf-8")
        rc = main([str(doc), "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "blocking" in data
        assert rc == 0

    def test_cli_strict_mode(self, capsys, tmp_path):
        from tenir_governance.copy_lint import main
        doc = tmp_path / "s.md"
        doc.write_text("The canonical approach here.\n", encoding="utf-8")
        rc = main([str(doc), "--strict"])
        assert rc == 1

    def test_cli_exposure_override(self, capsys, tmp_path):
        from tenir_governance.copy_lint import main
        doc = tmp_path / "ov.md"
        doc.write_text("canonical approach.\n", encoding="utf-8")
        rc = main([str(doc), "--exposure", "public"])
        assert rc == 1


# ─── LEDGER MIGRATE ──────────────────────────────────────────────────────────

from tenir_governance.ledger_migrate import (
    migrate_ledger, verify_migrated_ledger, MigrationReport,
    _compute_chain_hash, _rewrite_value, _walk_and_rewrite,
)


class TestComputeChainHash:
    def test_deterministic(self):
        h1 = _compute_chain_hash("GENESIS", {"key": "value"})
        h2 = _compute_chain_hash("GENESIS", {"key": "value"})
        assert h1 == h2

    def test_different_payloads_differ(self):
        h1 = _compute_chain_hash("GENESIS", {"key": "A"})
        h2 = _compute_chain_hash("GENESIS", {"key": "B"})
        assert h1 != h2

    def test_different_previous_hash_differs(self):
        h1 = _compute_chain_hash("GENESIS", {"key": "v"})
        h2 = _compute_chain_hash("OTHER", {"key": "v"})
        assert h1 != h2


class TestRewriteValue:
    def test_ces_key_schizophrenia_renamed(self):
        new_val, note = _rewrite_value("ces_state", "SCHIZOPHRENIA")
        assert new_val == "SIGNAL_CONFLICT"
        assert note is not None

    def test_ces_key_already_canonical(self):
        new_val, note = _rewrite_value("ces_state", "SIGNAL_CONFLICT")
        assert new_val == "SIGNAL_CONFLICT"
        assert note is None

    def test_frame_type_renamed(self):
        new_val, note = _rewrite_value("frame_type", "SCHIZOPHRENIA_ALERT")
        assert new_val == "SIGNAL_CONFLICT_ALERT"
        assert note is not None

    def test_rationale_text_schizophrenia_replaced(self):
        new_val, note = _rewrite_value("rationale", "State is SCHIZOPHRENIA here")
        assert "SIGNAL_CONFLICT" in new_val
        assert note is not None

    def test_rationale_text_whale_resonance_replaced(self):
        new_val, note = _rewrite_value("rationale", "Detected WHALE_RESONANCE event")
        assert "DEEP_PATTERN_SIGNAL" in new_val
        assert note is not None

    def test_unrelated_key_unchanged(self):
        new_val, note = _rewrite_value("actor_id", "operator-01")
        assert new_val == "operator-01"
        assert note is None

    def test_non_string_value_passthrough(self):
        new_val, note = _rewrite_value("ces_state", 42)
        assert new_val == 42
        assert note is None


class TestWalkAndRewrite:
    def test_nested_dict_rewrite(self):
        report = MigrationReport(source_path="test.jsonl")
        payload = {"ces_state": "SCHIZOPHRENIA", "sub": {"ces_state": "REST"}}
        result = _walk_and_rewrite(payload, report)
        assert result["ces_state"] == "SIGNAL_CONFLICT"
        assert result["sub"]["ces_state"] == "REST"

    def test_list_payload_rewrite(self):
        report = MigrationReport(source_path="test.jsonl")
        payload = [{"ces_state": "SCHIZOPHRENIA"}, {"ces_state": "REST"}]
        result = _walk_and_rewrite(payload, report)
        assert result[0]["ces_state"] == "SIGNAL_CONFLICT"
        assert result[1]["ces_state"] == "REST"

    def test_scalar_passthrough(self):
        report = MigrationReport(source_path="test.jsonl")
        assert _walk_and_rewrite("plain string", report) == "plain string"


class TestMigrateLedger:
    def _make_legacy_ledger(self, path: Path, ces_states: list) -> None:
        """Write a JSONL ledger with given CES states."""
        import hashlib, json as _json
        prev = "GENESIS"
        with path.open("w") as f:
            for i, state in enumerate(ces_states):
                payload = {"ces_state": state, "index": i, "s_score": 1.5}
                clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
                chain_hash = hashlib.sha256(f"{prev}|{clean}".encode()).hexdigest()
                entry = {"previous_hash": prev, "chain_hash": chain_hash,
                         "timestamp": "2026-01-01T00:00:00Z", "payload": payload}
                f.write(_json.dumps(entry) + "\n")
                prev = chain_hash

    def test_dry_run_does_not_modify_file(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        self._make_legacy_ledger(ledger, ["REST", "SCHIZOPHRENIA", "REST"])
        original_content = ledger.read_text()
        report = migrate_ledger(ledger, dry_run=True)
        assert ledger.read_text() == original_content
        assert report.entries_scanned == 3

    def test_migration_renames_schizophrenia(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        self._make_legacy_ledger(ledger, ["SCHIZOPHRENIA", "REST"])
        report = migrate_ledger(ledger, dry_run=False)
        assert report.entries_rewritten >= 1
        assert (tmp_path / "ledger.jsonl.pre-migration").exists()
        # Check actual ces_state values in payload lines (skip genesis header)
        lines = ledger.read_text().strip().splitlines()
        payload_states = []
        for line in lines:
            entry = json.loads(line)
            payload = entry.get("payload", {})
            if "ces_state" in payload:
                payload_states.append(payload["ces_state"])
        assert "SCHIZOPHRENIA" not in payload_states
        assert "SIGNAL_CONFLICT" in payload_states

    def test_migration_creates_backup(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        self._make_legacy_ledger(ledger, ["REST"])
        migrate_ledger(ledger, dry_run=False)
        assert (tmp_path / "ledger.jsonl.pre-migration").exists()

    def test_migration_clean_ledger_no_renames(self, tmp_path):
        ledger = tmp_path / "clean.jsonl"
        self._make_legacy_ledger(ledger, ["REST", "TENSION"])
        report = migrate_ledger(ledger, dry_run=False)
        assert report.entries_rewritten == 0

    def test_migration_nonexistent_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            migrate_ledger(tmp_path / "ghost.jsonl")

    def test_migration_empty_ledger_returns_early(self, tmp_path):
        ledger = tmp_path / "empty.jsonl"
        ledger.write_text("", encoding="utf-8")
        report = migrate_ledger(ledger, dry_run=False)
        assert report.entries_scanned == 0

    def test_migration_summary_output(self, tmp_path):
        ledger = tmp_path / "s.jsonl"
        self._make_legacy_ledger(ledger, ["SCHIZOPHRENIA"])
        report = migrate_ledger(ledger, dry_run=True)
        summary = report.summary()
        assert isinstance(summary, str) and len(summary) > 0


class TestVerifyMigratedLedger:
    def _migrate_and_verify(self, tmp_path, states):
        import hashlib, json as _json
        from tenir_governance.ledger_migrate import migrate_ledger, verify_migrated_ledger
        ledger = tmp_path / "mig.jsonl"
        prev = "GENESIS"
        with ledger.open("w") as f:
            for i, state in enumerate(states):
                payload = {"ces_state": state, "i": i}
                clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
                h = hashlib.sha256(f"{prev}|{clean}".encode()).hexdigest()
                entry = {"previous_hash": prev, "chain_hash": h,
                         "timestamp": "T", "payload": payload}
                f.write(_json.dumps(entry) + "\n")
                prev = h
        migrate_ledger(ledger, dry_run=False)
        return verify_migrated_ledger(ledger)

    def test_valid_migrated_ledger(self, tmp_path):
        ok, msg = self._migrate_and_verify(tmp_path, ["REST", "TENSION"])
        assert ok is True
        assert "Valid" in msg

    def test_non_migrated_ledger_fails(self, tmp_path):
        """A file without a migration genesis should fail."""
        import hashlib, json as _json
        ledger = tmp_path / "raw.jsonl"
        payload = {"ces_state": "REST"}
        clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
        h = hashlib.sha256(f"GENESIS|{clean}".encode()).hexdigest()
        ledger.write_text(_json.dumps({"previous_hash": "GENESIS",
                                        "chain_hash": h, "payload": payload}) + "\n")
        ok, msg = verify_migrated_ledger(ledger)
        assert ok is False
        assert "migration genesis" in msg


class TestLedgerMigrateCLI:
    def _write_ledger(self, path, states):
        import hashlib, json as _json
        prev = "GENESIS"
        with path.open("w") as f:
            for i, st in enumerate(states):
                payload = {"ces_state": st, "i": i}
                clean = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
                h = hashlib.sha256(f"{prev}|{clean}".encode()).hexdigest()
                f.write(_json.dumps({"previous_hash": prev, "chain_hash": h, "payload": payload}) + "\n")
                prev = h

    def test_cli_dry_run(self, capsys, tmp_path):
        from tenir_governance.ledger_migrate import main
        ledger = tmp_path / "l.jsonl"
        self._write_ledger(ledger, ["SCHIZOPHRENIA", "REST"])
        rc = main([str(ledger), "--dry-run"])
        assert rc == 0

    def test_cli_migrate(self, capsys, tmp_path):
        from tenir_governance.ledger_migrate import main
        ledger = tmp_path / "l.jsonl"
        self._write_ledger(ledger, ["SCHIZOPHRENIA"])
        rc = main([str(ledger)])
        assert rc == 0

    def test_cli_verify(self, capsys, tmp_path):
        from tenir_governance.ledger_migrate import main, migrate_ledger
        ledger = tmp_path / "l.jsonl"
        self._write_ledger(ledger, ["REST"])
        migrate_ledger(ledger)
        rc = main([str(ledger), "--verify"])
        assert rc == 0

    def test_cli_verify_json(self, capsys, tmp_path):
        from tenir_governance.ledger_migrate import main, migrate_ledger
        ledger = tmp_path / "l.jsonl"
        self._write_ledger(ledger, ["TENSION"])
        migrate_ledger(ledger)
        rc = main([str(ledger), "--verify", "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["valid"] is True
        assert rc == 0

    def test_cli_json_report(self, capsys, tmp_path):
        from tenir_governance.ledger_migrate import main
        ledger = tmp_path / "j.jsonl"
        self._write_ledger(ledger, ["REST"])
        rc = main([str(ledger), "--dry-run", "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "entries_scanned" in data
        assert rc == 0

    def test_cli_missing_file_exits_2(self, capsys, tmp_path):
        from tenir_governance.ledger_migrate import main
        rc = main([str(tmp_path / "ghost.jsonl")])
        assert rc == 2


# ─── REGRESSION CORPUS HELPERS ──────────────────────────────────────────────

from tenir_governance.regression_corpus import (
    CORPUS, get_case, scenario_group, tagged, nsl_cases,
)


class TestRegressionCorpusHelpers:
    def test_get_case_valid_id(self):
        first_id = CORPUS[0].id
        case = get_case(first_id)
        assert case.id == first_id

    def test_get_case_invalid_id_raises(self):
        with pytest.raises(KeyError):
            get_case("NONEXISTENT-CASE-9999")

    def test_scenario_group_returns_subset(self):
        if not CORPUS:
            pytest.skip("Empty corpus")
        group_name = CORPUS[0].scenario_group
        group = scenario_group(group_name)
        assert len(group) >= 1
        assert all(c.scenario_group == group_name for c in group)

    def test_scenario_group_nonexistent_returns_empty(self):
        result = scenario_group("NONEXISTENT_GROUP_XYZ")
        assert result == []

    def test_tagged_returns_subset(self):
        all_tags = set()
        for c in CORPUS:
            all_tags.update(c.tags)
        if not all_tags:
            pytest.skip("No tags in corpus")
        tag = next(iter(all_tags))
        result = tagged(tag)
        assert all(tag in c.tags for c in result)

    def test_tagged_nonexistent_returns_empty(self):
        result = tagged("NONEXISTENT_TAG_9999")
        assert result == []

    def test_nsl_cases_returns_subset_with_nsl(self):
        result = nsl_cases()
        assert all(c.nsl_input is not None for c in result)
