"""Connector for Python agent loops and ordinary callable tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ..kernel import TenirKernel

T = TypeVar("T")


class GovernanceBlockedError(RuntimeError):
    """Raised when ENFORCE mode returns a hard veto before a tool is called."""

    def __init__(self, decision: Mapping[str, Any]) -> None:
        self.decision = dict(decision)
        super().__init__(str(self.decision.get("rationale", "TENIR blocked execution.")))


class PythonConnector:
    """Evaluate a TENIR 1.2 object before entering a Python side-effect boundary.

    Shadow modes report intended blocks but continue to the callable. ENFORCE
    skips the callable whenever the kernel returns ``execution_allowed=False``.
    """

    def __init__(self, kernel: TenirKernel | None = None, *, mode: str = "SHADOW_PASSIVE") -> None:
        self.kernel = kernel or TenirKernel()
        self.mode = mode

    def adjudicate(self, runtime_object: Mapping[str, Any]) -> dict[str, Any]:
        return self.kernel.adjudicate_runtime_object(runtime_object, mode=self.mode)

    def execute(
        self,
        runtime_object: Mapping[str, Any],
        action: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[T, dict[str, Any]]:
        decision = self.adjudicate(runtime_object)
        if not decision["execution_allowed"]:
            raise GovernanceBlockedError(decision)
        return action(*args, **kwargs), decision
