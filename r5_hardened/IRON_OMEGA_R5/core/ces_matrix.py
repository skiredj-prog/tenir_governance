from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CESState:
    REST: str = "REST"
    TENSION: str = "TENSION"
    METABOLIZING: str = "METABOLIZING"
    SCHIZOPHRENIA: str = "SCHIZOPHRENIA"
    COLLAPSE: str = "COLLAPSE"


class CESMatrix:
    """Placeholder CES container for the intake workspace.

    The current R5 server only instantiates this object; it does not call into
    a richer CES engine. Keeping the class explicit makes that boundary honest.
    """

    def __init__(self) -> None:
        self.states = CESState()
