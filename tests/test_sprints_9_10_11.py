"""
Tests for Polymorphic Surface, Copy-Lint, and Ledger Migration
================================================================
Sprints 9, 10, 11 verification.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from tenir_governance import (
    # Polymorphic Surface
    SurfaceState, Persona, SurfaceContext, SurfaceFrame,
    recommend_surface_state, is_lawful_transition, requires_ceremony,
    build_surface_frame, can_cross_bridge,
    STATE_OPACITY, SURFACE_ACCESS,
    # Nomenclature
    CESStateNames, MembraneDecisionNames, OperatingModeNames,
    # Tooling
    CopyLinter, migrate_ledger, verify_migrated_ledger,
)


# ─── POLYMORPHIC SURFACE ─────────────────────────────────────────────────────

class TestSurfaceStateRecommender:
    """Sprint 10 — surface state recommender follows V5 doctrine."""

    def _ctx(self, **overrides) -> SurfaceContext:
        defaults = dict(
            operating_mode=OperatingModeNames.SHADOW_PASSIVE,
            membrane_decision=MembraneDecisionNames.ALLOW,
            ces_state=CESStateNames.REST,
            persona=Persona.EXECUTIVE,
            simulation_active=False,
            audit_requested=False,
        )
        defaults.update(overrides)
        return SurfaceContext(**defaults)

    def test_ambient_is_default(self):
        assert recommend_surface_state(self._ctx()) == SurfaceState.AMBIENT

    def test_block_decision_routes_to_adjudication(self):
        ctx = self._ctx(membrane_decision=MembraneDecisionNames.BLOCK)
        assert recommend_surface_state(ctx) == SurfaceState.ADJUDICATION

    def test_intended_block_routes_to_adjudication(self):
        ctx = self._ctx(membrane_decision=MembraneDecisionNames.ALLOW_WITH_INTENDED_BLOCK)
        assert recommend_surface_state(ctx) == SurfaceState.ADJUDICATION

    def test_collapse_state_routes_to_adjudication(self):
        ctx = self._ctx(ces_state=CESStateNames.COLLAPSE)
        assert recommend_surface_state(ctx) == SurfaceState.ADJUDICATION

    def test_audit_routes_to_forensic(self):
        ctx = self._ctx(audit_requested=True)
        assert recommend_surface_state(ctx) == SurfaceState.FORENSIC

    def test_simulation_routes_to_anticipation(self):
        ctx = self._ctx(simulation_active=True)
        assert recommend_surface_state(ctx) == SurfaceState.ANTICIPATION

    def test_adjudication_takes_precedence_over_audit(self):
        ctx = self._ctx(
            membrane_decision=MembraneDecisionNames.BLOCK,
            audit_requested=True,
        )
        assert recommend_surface_state(ctx) == SurfaceState.ADJUDICATION

    def test_adjudication_takes_precedence_over_simulation(self):
        ctx = self._ctx(
            ces_state=CESStateNames.COLLAPSE,
            simulation_active=True,
        )
        assert recommend_surface_state(ctx) == SurfaceState.ADJUDICATION


class TestSurfaceTransitions:
    """Sprint 10 — transition legality and ceremony requirements."""

    def test_self_transition_is_always_lawful(self):
        for state in SurfaceState.ALL:
            assert is_lawful_transition(state, state)

    def test_ambient_to_anticipation_is_lawful(self):
        assert is_lawful_transition(SurfaceState.AMBIENT, SurfaceState.ANTICIPATION)

    def test_adjudication_to_ambient_requires_ceremony(self):
        assert is_lawful_transition(SurfaceState.ADJUDICATION, SurfaceState.AMBIENT)
        assert requires_ceremony(SurfaceState.ADJUDICATION, SurfaceState.AMBIENT)

    def test_ambient_to_adjudication_does_not_require_ceremony(self):
        # Automatic crisis response — no confirmation
        assert is_lawful_transition(SurfaceState.AMBIENT, SurfaceState.ADJUDICATION)
        assert not requires_ceremony(SurfaceState.AMBIENT, SurfaceState.ADJUDICATION)

    def test_forensic_to_elsewhere_is_free(self):
        # FORENSIC is read-only; leaving never requires ceremony
        for target in SurfaceState.ALL:
            if target != SurfaceState.FORENSIC:
                assert is_lawful_transition(SurfaceState.FORENSIC, target)
                assert not requires_ceremony(SurfaceState.FORENSIC, target)


class TestSurfaceAccess:
    """Sprint 10 — persona access control."""

    def test_all_personas_may_view_ambient(self):
        for persona in Persona.ALL:
            frame = build_surface_frame(
                SurfaceState.AMBIENT, OperatingModeNames.SHADOW_PASSIVE,
                persona, "v1.0.0", "fp123",
            )
            assert frame.state == SurfaceState.AMBIENT

    def test_only_iron_plus_executive_can_act_in_adjudication(self):
        for persona in Persona.IRON | {Persona.EXECUTIVE}:
            assert persona in SURFACE_ACCESS[SurfaceState.ADJUDICATION]["act"]

        # Business Leader and Operational Manager cannot act in adjudication
        assert Persona.BUSINESS_LEADER not in SURFACE_ACCESS[SurfaceState.ADJUDICATION]["act"]
        assert Persona.OPERATIONAL_MANAGER not in SURFACE_ACCESS[SurfaceState.ADJUDICATION]["act"]

    def test_forensic_view_restricted(self):
        # Only IRON personas + Executive may view forensic
        for persona in Persona.IRON | {Persona.EXECUTIVE}:
            assert persona in SURFACE_ACCESS[SurfaceState.FORENSIC]["view"]
        for persona in {Persona.BUSINESS_LEADER, Persona.OPERATIONAL_MANAGER}:
            assert persona not in SURFACE_ACCESS[SurfaceState.FORENSIC]["view"]

    def test_unauthorized_persona_raises_on_frame_build(self):
        with pytest.raises(PermissionError):
            build_surface_frame(
                SurfaceState.FORENSIC,
                OperatingModeNames.SHADOW_PASSIVE,
                Persona.BUSINESS_LEADER,  # not authorized for FORENSIC
                "v1.0.0", "fp123",
            )


class TestTwoGlassBridge:
    """Sprint 10 — Iron↔Glass bridge authority."""

    def test_iron_personas_can_view_glass(self):
        for persona in Persona.IRON:
            assert can_cross_bridge(persona, "iron_to_glass")

    def test_only_executive_can_cross_glass_to_iron(self):
        assert can_cross_bridge(Persona.EXECUTIVE, "glass_to_iron")
        assert not can_cross_bridge(Persona.BUSINESS_LEADER, "glass_to_iron")
        assert not can_cross_bridge(Persona.OPERATIONAL_MANAGER, "glass_to_iron")


class TestOpacityMap:
    """Sprint 10 — state opacity ordering (AMBIENT lightest, ADJUDICATION heaviest)."""

    def test_ambient_is_most_transparent(self):
        assert STATE_OPACITY[SurfaceState.AMBIENT] == min(STATE_OPACITY.values())

    def test_adjudication_is_most_opaque(self):
        assert STATE_OPACITY[SurfaceState.ADJUDICATION] == max(STATE_OPACITY.values())

    def test_all_states_in_zero_one_range(self):
        for opacity in STATE_OPACITY.values():
            assert 0.0 <= opacity <= 1.0


# ─── COPY-LINT ────────────────────────────────────────────────────────────────

class TestCopyLint:
    """Sprint 9 — copy-lint catches banned terms and deprecated CAVE."""

    def test_clean_public_file_has_no_findings(self, tmp_path):
        f = tmp_path / "clean.md"
        f.write_text(
            "---\nexposure: public\n---\n\n"
            "# Welcome\n\nThis is a governance system.\n"
        )
        report = CopyLinter().lint_file(f)
        assert not report.findings, report.summary()

    def test_banned_term_blocks_public(self, tmp_path):
        f = tmp_path / "dirty.md"
        f.write_text(
            "---\nexposure: public\n---\n\n"
            "Our doctrine protects your estate.\n"
        )
        report = CopyLinter().lint_file(f)
        assert report.blocking
        assert report.block_count >= 2  # doctrine + estate

    def test_banned_term_is_only_warning_in_operator(self, tmp_path):
        f = tmp_path / "ops.md"
        f.write_text(
            "---\nexposure: operator\n---\n\n"
            "Our doctrine protects your estate.\n"
        )
        report = CopyLinter().lint_file(f)
        assert not report.blocking
        assert report.warn_count >= 2

    def test_deprecated_cave_always_blocks(self, tmp_path):
        f = tmp_path / "cave.md"
        f.write_text(
            "The CAVE matrix (Control · Aim · Veto · Epistemics) governs.\n",
            encoding="utf-8"
        )
        report = CopyLinter().lint_file(f)
        assert report.blocking
        assert any(f.category == "deprecated_cave" for f in report.findings)

    def test_strict_mode_blocks_all_banned(self, tmp_path):
        f = tmp_path / "ops.md"
        f.write_text("Our estate and doctrine.\n")
        report = CopyLinter(strict=True).lint_file(f)
        assert report.blocking


# ─── LEDGER MIGRATION ─────────────────────────────────────────────────────────

class TestLedgerMigration:
    """Sprint 11 — legacy label rename preserves chain integrity."""

    def _write_legacy(self, path: Path) -> None:
        entries = [
            {
                "previous_hash": "GENESIS",
                "chain_hash": "h1",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "payload": {
                    "type": "observation",
                    "ces_state": "SCHIZOPHRENIA",
                    "rationale": "contradictory signals",
                },
            },
            {
                "previous_hash": "h1",
                "chain_hash": "h2",
                "timestamp": "2026-01-02T00:00:00+00:00",
                "payload": {
                    "type": "observation",
                    "ces_state": "METABOLIZING",
                    "rationale": "WHALE_RESONANCE detected",
                },
            },
        ]
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_migration_renames_ces_state(self, tmp_path):
        ledger = tmp_path / "legacy.jsonl"
        self._write_legacy(ledger)
        report = migrate_ledger(ledger)
        assert report.entries_rewritten >= 1
        assert any("SCHIZOPHRENIA" in k for k in report.renames_by_key)

    def test_migrated_ledger_verifies(self, tmp_path):
        ledger = tmp_path / "legacy.jsonl"
        self._write_legacy(ledger)
        migrate_ledger(ledger)
        ok, msg = verify_migrated_ledger(ledger)
        assert ok, msg

    def test_migration_preserves_backup(self, tmp_path):
        ledger = tmp_path / "legacy.jsonl"
        self._write_legacy(ledger)
        migrate_ledger(ledger)
        backup = ledger.with_suffix(ledger.suffix + ".pre-migration")
        assert backup.exists()

    def test_dry_run_does_not_write(self, tmp_path):
        ledger = tmp_path / "legacy.jsonl"
        self._write_legacy(ledger)
        original_content = ledger.read_text()
        report = migrate_ledger(ledger, dry_run=True)
        assert report.entries_rewritten >= 1
        # File unchanged
        assert ledger.read_text() == original_content
        # No backup created
        assert not ledger.with_suffix(ledger.suffix + ".pre-migration").exists()

    def test_migration_genesis_is_first_entry(self, tmp_path):
        ledger = tmp_path / "legacy.jsonl"
        self._write_legacy(ledger)
        migrate_ledger(ledger)
        with ledger.open() as f:
            first = json.loads(f.readline())
        assert first["payload"]["type"] == "legacy_label_migration"
        assert first["previous_hash"] == "GENESIS"
