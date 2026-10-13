"""Smoke tests: package importability, version, and public API surface.

These tests verify that the published package is importable and that the
key public symbols are accessible. They run as part of the normal test
suite — no special flags needed.
"""
from __future__ import annotations

import tenir_governance


class TestPackageIntegrity:
    """Verify the package is importable and exposes its public API."""

    def test_version_is_set(self):
        assert tenir_governance.__version__
        assert isinstance(tenir_governance.__version__, str)

    def test_policy_version_is_set(self):
        assert tenir_governance.__policy_version__

    def test_profile_version_is_set(self):
        assert tenir_governance.__profile_version__

    def test_schema_version_is_set(self):
        assert tenir_governance.__schema_version__

    def test_governor_importable(self):
        from tenir_governance.composition.governor import DualKernelGovernor
        assert DualKernelGovernor is not None

    def test_sdk_importable(self):
        from tenir_governance.sdk import DualKernelCompositionClient
        assert DualKernelCompositionClient is not None

    def test_membrane_importable(self):
        from tenir_governance.membrane import TransitionMembrane
        assert TransitionMembrane is not None

    def test_provider_importable(self):
        from tenir_governance.operations.provider import MockProvider
        assert MockProvider is not None

    def test_hold_manager_importable(self):
        from tenir_governance.operations.hold import HoldManager
        assert HoldManager is not None

    def test_assessment_importable(self):
        from tenir_governance.assessment import (
            TauInversionClassifier,
            EVPAssessor,
            PostExecutionAssessor,
        )
        assert TauInversionClassifier is not None
        assert EVPAssessor is not None
        assert PostExecutionAssessor is not None