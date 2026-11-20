"""The examples of docs/agentic-governance-guide.md run, top to bottom.

A guide whose examples fail states behaviour nobody observed. Each Python
block is executed in order, in one namespace, as a reader would run them.
"""
import contextlib
import io
import re
import warnings
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "agentic-governance-guide.md"


def test_guide_examples_run_as_written():
    blocks = re.findall(r"```python\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.S)
    assert len(blocks) >= 8
    namespace: dict = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i, block in enumerate(blocks):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    exec(compile(block, f"{GUIDE.name}[block {i}]", "exec"), namespace)
                except Exception as exc:  # pragma: no cover - message only
                    raise AssertionError(f"guide block {i} fails: {exc!r}") from exc
