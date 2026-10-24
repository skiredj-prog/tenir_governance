"""Cross-process reproducibility gates for the versioned source checkout.

These tests need no unpublished /workspace or alternate site-packages copy.
"""
from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

from tenir_governance.objects import Resource
from tenir_governance.operations.budget import DurableBudgetCoordinator


def test_subprocess_imports_this_checkout_not_an_external_fixed_copy():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, tenir_governance; print(json.dumps({'origin': tenir_governance.__file__}))",
        ],
        cwd=str(root),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    actual = Path(json.loads(result.stdout)["origin"]).resolve()
    assert actual == (root / "tenir_governance" / "__init__.py").resolve()


def test_decimal_get_state_has_explicit_lossless_json_adapter(tmp_path):
    coordinator = DurableBudgetCoordinator(str(tmp_path / "state.json"))
    coordinator.register(Resource(resource_id="budget", capacity=100.0))
    assert coordinator.reserve("budget", Decimal("30.0"))
    state = coordinator.get_state("budget")
    assert isinstance(state["available"], Decimal)
    assert state["available"] == Decimal("70.0")
    # JSON transport representation is explicit; the accounting contract
    # remains exact Decimal and must never be silently coerced to int.
    wire = json.dumps({key: str(value) for key, value in state.items()})
    restored = {key: Decimal(value) for key, value in json.loads(wire).items()}
    assert restored == state
