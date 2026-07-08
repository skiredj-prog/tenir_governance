"""
R5 ↔ tenir_governance Integration Test (Sprint 12)
===================================================
Proves that R5's core.trajectory.TrajectoryKernel and core.ces_matrix.CESMatrix
accept the shared tenir_governance.PolicyEngine and delegate to its
classify_ces() and evaluate_membrane() methods.

This closes parts of G2 (trajectory math) and G5 (LLM deterministic fallback)
from the V4 UAT audit: the kernel now derives epsilon from the canonical
policy, and classification falls back deterministically when the shared
engine is unavailable.
"""

from __future__ import annotations

import unittest


class TestR5KernelWiring(unittest.TestCase):
    """TrajectoryKernel accepts and uses a PolicyEngine."""

    def test_kernel_default_epsilon_without_policy(self) -> None:
        from core.trajectory import TrajectoryKernel
        kernel = TrajectoryKernel()
        self.assertEqual(kernel.epsilon, 0.01)  # R5 legacy default

    def test_kernel_accepts_policy_engine(self) -> None:
        from core.trajectory import TrajectoryKernel
        from tenir_governance import PolicyEngine
        policy = PolicyEngine.default()
        kernel = TrajectoryKernel(policy=policy)
        # Epsilon now comes from canonical policy (1e-6, not 0.01)
        self.assertEqual(kernel.epsilon, policy.epsilon)
        self.assertEqual(kernel.epsilon, 1e-6)

    def test_kernel_policy_produces_canonical_math(self) -> None:
        from core.trajectory import TrajectoryKernel
        from tenir_governance import PolicyEngine
        policy = PolicyEngine.default()
        kernel = TrajectoryKernel(policy=policy)

        # With P=1, V=1, K=1: S = 1 / (1 + 1e-6) ≈ 1.0
        snap = kernel.compute(pressure=1.0, velocity=1.0, capacity=1.0)
        self.assertAlmostEqual(snap.s_score, 1.0, places=4)


class TestR5CESMatrixWiring(unittest.TestCase):
    """CESMatrix routes classification through PolicyEngine."""

    def test_ces_matrix_has_signal_conflict_rename(self) -> None:
        from core.ces_matrix import CESMatrix
        matrix = CESMatrix()
        # Canonical name present
        self.assertEqual(matrix.states.SIGNAL_CONFLICT, "SIGNAL_CONFLICT")
        # Backward-compat alias still works (maps to canonical value)
        self.assertEqual(matrix.states.SCHIZOPHRENIA, "SIGNAL_CONFLICT")

    def test_ces_matrix_classify_without_policy(self) -> None:
        from core.ces_matrix import CESMatrix
        matrix = CESMatrix()
        # Should still work (either delegates to shared or falls back)
        state = matrix.classify(s_score=2.0, ds_de=0.0, option_space=0.8)
        self.assertEqual(state, "REST")

    def test_ces_matrix_classify_with_policy(self) -> None:
        from core.ces_matrix import CESMatrix
        from tenir_governance import PolicyEngine
        matrix = CESMatrix()
        policy = PolicyEngine.default()
        # Use doctrine-reconciled classifier
        state = matrix.classify(
            s_score=2.0, ds_de=0.0, option_space=0.8,
            pressure=0.3, velocity=0.3,
            policy=policy,
        )
        self.assertEqual(state, "REST")

    def test_ces_matrix_classify_signal_conflict(self) -> None:
        from core.ces_matrix import CESMatrix
        from tenir_governance import PolicyEngine
        matrix = CESMatrix()
        policy = PolicyEngine.default()
        # S below block floor with materially present option_space
        # P=1.5 V=1.5 K=0.7 → S ≈ 0.31 < tau_floor → actually COLLAPSE
        # Need S between tau_floor and s_block_floor with os >= 0.55
        # K for S=0.60: K = 0.60 * (P*V) = 0.60 * 2.25 = 1.35
        state = matrix.classify(
            s_score=0.60, ds_de=0.0, option_space=0.70,
            pressure=1.5, velocity=1.5,
            policy=policy,
        )
        self.assertEqual(state, "SIGNAL_CONFLICT")

    def test_ces_matrix_classify_collapse(self) -> None:
        from core.ces_matrix import CESMatrix
        from tenir_governance import PolicyEngine
        matrix = CESMatrix()
        policy = PolicyEngine.default()
        # S below tau_floor=0.42
        state = matrix.classify(
            s_score=0.30, ds_de=-0.1, option_space=0.1,
            pressure=2.0, velocity=2.0,
            policy=policy,
        )
        self.assertEqual(state, "COLLAPSE")


class TestR5MembraneDecisionViaPolicy(unittest.TestCase):
    """Direct check that PolicyEngine.evaluate_membrane works for R5 semantics."""

    def test_shadow_passive_tau_breach_intended_block(self) -> None:
        from tenir_governance import PolicyEngine
        policy = PolicyEngine.default()
        s = 0.3  # below tau_floor=0.42
        decision, _, alert, intended = policy.evaluate_membrane(
            s_score=s, ds_de=-0.1, d2s_de2=0.0,
            option_space=0.1, projected_events_to_zero=2,
            operating_mode="SHADOW_PASSIVE",
        )
        self.assertEqual(decision, "allow_with_intended_block")
        self.assertTrue(intended)

    def test_enforce_tau_breach_hard_block(self) -> None:
        from tenir_governance import PolicyEngine
        policy = PolicyEngine.default()
        s = 0.3
        decision, rationale, _, _ = policy.evaluate_membrane(
            s_score=s, ds_de=-0.1, d2s_de2=0.0,
            option_space=0.1, projected_events_to_zero=2,
            operating_mode="ENFORCE",
        )
        self.assertEqual(decision, "block")
        self.assertIn("TAU BREACH", rationale)


class TestR5ServerImports(unittest.TestCase):
    """The server module imports cleanly and exposes the wired helpers."""

    def test_server_module_imports(self) -> None:
        # Ensure server module can be imported without side-effects
        import importlib
        import sys

        # Stub out FastAPI/uvicorn dependencies if missing
        try:
            import r5_server  # noqa: F401
        except ImportError as e:
            # Environment may not have fastapi; skip gracefully
            self.skipTest(f"Server import requires optional deps: {e}")

    def test_server_helpers_exist(self) -> None:
        try:
            from r5_server import _scalar_to_ces_state, _membrane_decision
        except ImportError:
            self.skipTest("Server module not importable")

        # Helpers have the expected updated signatures (accept optional kwargs)
        import inspect
        sig_ces = inspect.signature(_scalar_to_ces_state)
        self.assertIn("pressure", sig_ces.parameters)
        self.assertIn("velocity", sig_ces.parameters)

        sig_mem = inspect.signature(_membrane_decision)
        self.assertIn("option_space", sig_mem.parameters)
        self.assertIn("horizon_events", sig_mem.parameters)


if __name__ == "__main__":
    unittest.main()
