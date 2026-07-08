from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CESState:
    REST:            str = "REST"
    TENSION:         str = "TENSION"
    METABOLIZING:    str = "METABOLIZING"
    SIGNAL_CONFLICT: str = "SIGNAL_CONFLICT"   # canonical rename
    SCHIZOPHRENIA:   str = "SIGNAL_CONFLICT"   # backward-compat alias
    COLLAPSE:        str = "COLLAPSE"


class CESMatrix:
    """Bridge to shared PolicyEngine.classify_ces().

    Delegates to the canonical classifier when tenir_governance is installed.
    Falls back to a coarse S-only heuristic so that r5_wired can run in
    isolation (e.g. unit-test environments) without requiring the full
    governance package.

    FIX 2026-06-25: removed pressure= and velocity= kwargs from the
    classify_ces() call.  PolicyEngine.classify_ces() signature is:
        (self, s_score, ds_de, option_space) -> str
    Passing unsupported kwargs caused TypeError when r5_wired was used
    against the canonical governance package outside the full stack.
    Both kwargs are still accepted by classify() so callers need not change.
    """

    def __init__(self) -> None:
        self.states = CESState()

    def classify(
        self,
        s_score: float,
        ds_de: float,
        option_space: Optional[float] = None,
        pressure: Optional[float] = None,   # kept for caller compatibility
        velocity: Optional[float] = None,   # kept for caller compatibility
        policy=None,
    ) -> str:
        try:
            from tenir_governance import PolicyEngine
            engine = policy if policy is not None else PolicyEngine.default()
            return engine.classify_ces(s_score, ds_de, option_space)
        except ImportError:
            if s_score >= 1.5:
                return self.states.REST
            if s_score >= 0.9:
                return self.states.METABOLIZING
            if s_score >= 0.75:
                return self.states.TENSION
            if s_score >= 0.42:
                return self.states.SIGNAL_CONFLICT
            return self.states.COLLAPSE
