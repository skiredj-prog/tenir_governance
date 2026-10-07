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
