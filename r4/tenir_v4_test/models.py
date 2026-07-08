from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


def _ensure_finite_number(
    name: str,
    value: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")


def _ensure_iso_timestamp(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty ISO-8601 string")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone information")


class OperatingMode(str, Enum):
    SHADOW_OFF = "SHADOW_OFF"
    SHADOW_PASSIVE = "SHADOW_PASSIVE"
    SHADOW_CRITICAL = "SHADOW_CRITICAL"
    ENFORCE = "ENFORCE"

    @classmethod
    def parse(cls, value: "OperatingMode | str") -> "OperatingMode":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or not value.strip():
            raise TypeError("mode must be a non-empty string or OperatingMode")

        normalized = value.strip().upper().replace("-", "_")
        aliases = {
            "OFF": cls.SHADOW_OFF,
            "SHADOW_OFF": cls.SHADOW_OFF,
            "PASSIVE": cls.SHADOW_PASSIVE,
            "SHADOW": cls.SHADOW_PASSIVE,
            "SHADOW_PASSIVE": cls.SHADOW_PASSIVE,
            "CRITICAL": cls.SHADOW_CRITICAL,
            "SHADOW_CRITICAL": cls.SHADOW_CRITICAL,
            "ENFORCE": cls.ENFORCE,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            allowed = "', '".join(
                ["shadow-off", "shadow-passive", "shadow-critical", "enforce"]
            )
            raise ValueError(f"mode must be one of '{allowed}'") from exc

    @property
    def cli_value(self) -> str:
        return self.value.lower().replace("_", "-")


@dataclass(frozen=True)
class PolicyBundle:
    """Versioned runtime policy for the internal v4 test line.

    These thresholds are partner-policy values. They are not canon.

    WIRING NOTE (Sprint 12 — governance unification):
      PolicyBundle now includes a to_policy_engine() bridge method that
      returns the shared tenir_governance.PolicyEngine equivalent.
      TenirMonitor uses this to route every membrane decision through
      the shared PolicyEngine.evaluate_membrane() — no local thresholds.
    """

    version: str = "partner_a-shadow-v4-test-policy-2"
    epsilon: float = 1e-6
    s_alert_floor: float = 0.90
    s_block_floor: float = 0.75
    ds_de_alert_floor: float = -0.05
    d2s_de2_alert_floor: float = -0.03
    reaction_budget_events: int = 5
    event_window: int = 8
    option_space_alert_floor: float = 0.35
    option_space_block_floor: float = 0.20
    tau_floor: float = 0.42

    def validate(self) -> None:
        _ensure_finite_number("epsilon", self.epsilon, minimum=1e-15)
        _ensure_finite_number("s_alert_floor", self.s_alert_floor)
        _ensure_finite_number("s_block_floor", self.s_block_floor)
        _ensure_finite_number("ds_de_alert_floor", self.ds_de_alert_floor)
        _ensure_finite_number("d2s_de2_alert_floor", self.d2s_de2_alert_floor)
        _ensure_finite_number("tau_floor", self.tau_floor, minimum=0.0, maximum=1.0)
        if not isinstance(self.event_window, int):
            raise TypeError("event_window must be an integer")
        if not isinstance(self.reaction_budget_events, int):
            raise TypeError("reaction_budget_events must be an integer")
        if self.event_window < 3:
            raise ValueError("event_window must be at least 3")
        if self.reaction_budget_events < 1:
            raise ValueError("reaction_budget_events must be at least 1")
        _ensure_finite_number(
            "option_space_alert_floor",
            self.option_space_alert_floor,
            minimum=0.0,
            maximum=1.0,
        )
        _ensure_finite_number(
            "option_space_block_floor",
            self.option_space_block_floor,
            minimum=0.0,
            maximum=1.0,
        )
        if self.s_block_floor > self.s_alert_floor:
            raise ValueError("s_block_floor must be <= s_alert_floor")
        if self.option_space_block_floor > self.option_space_alert_floor:
            raise ValueError(
                "option_space_block_floor must be <= option_space_alert_floor"
            )
        if self.tau_floor >= self.s_block_floor:
            raise ValueError("tau_floor must be < s_block_floor")

    def to_policy_engine(self):
        """
        Bridge to the shared governance package.

        Returns a tenir_governance.PolicyEngine with thresholds that mirror
        this PolicyBundle exactly. Used by TenirMonitor to route membrane
        decisions through PolicyEngine.evaluate_membrane().
        """
        try:
            from tenir_governance import PolicyEngine
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "PolicyBundle.to_policy_engine() requires the "
                "`tenir_governance` package. Install it from the "
                "governance hardening pack."
            ) from exc

        engine = PolicyEngine(
            version=self.version,
            scope="partner_a-shadow-v4-r4-runtime",
            tau_floor=self.tau_floor,
            epsilon=self.epsilon,
            s_alert_floor=self.s_alert_floor,
            s_block_floor=self.s_block_floor,
            ds_de_alert_floor=self.ds_de_alert_floor,
            d2s_de2_alert_floor=self.d2s_de2_alert_floor,
            reaction_budget_events=self.reaction_budget_events,
            event_window=self.event_window,
            option_space_alert_floor=self.option_space_alert_floor,
            option_space_block_floor=self.option_space_block_floor,
        )
        engine.validate()
        return engine


@dataclass(frozen=True)
class EventSample:
    """Single normalized observation event for the governance membrane."""

    pressure: float
    velocity: float
    capacity: float
    option_space: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def validate(self) -> None:
        for name, value in (
            ("pressure", self.pressure),
            ("velocity", self.velocity),
            ("capacity", self.capacity),
        ):
            _ensure_finite_number(name, value, minimum=0.0)
        if self.option_space is not None:
            _ensure_finite_number(
                "option_space",
                self.option_space,
                minimum=0.0,
                maximum=1.0,
            )
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata must be a dict")
        _ensure_iso_timestamp("observed_at", self.observed_at)


@dataclass(frozen=True)
class TrajectoryState:
    stability: float
    ds_de: float
    d2s_de2: float
    projected_events_to_zero: Optional[int]
    option_space_low: bool
    alert: bool
    intended_block: bool
    rationale: str


@dataclass(frozen=True)
class Verdict:
    mode: OperatingMode
    action: str
    trajectory: TrajectoryState
    observed_at: str
    ledger_path: str
    chain_hash: str


@dataclass(frozen=True)
class BurnInputs:
    """Optional commercial overlay parameters.

    The overlay reads the local ledger. It never governs runtime behavior.
    """

    cost_per_intended_block: float = 500.0
    cost_per_alert: float = 50.0
    review_minutes_per_alert: float = 12.0
    cost_per_review_minute: float = 4.0

    def validate(self) -> None:
        for name, value in (
            ("cost_per_intended_block", self.cost_per_intended_block),
            ("cost_per_alert", self.cost_per_alert),
            ("review_minutes_per_alert", self.review_minutes_per_alert),
            ("cost_per_review_minute", self.cost_per_review_minute),
        ):
            _ensure_finite_number(name, value, minimum=0.0)


@dataclass(frozen=True)
class BurnEstimate:
    intended_block_count: int
    alert_count: int
    cost_inputs: BurnInputs

    @property
    def review_cost(self) -> float:
        return round(
            self.alert_count
            * self.cost_inputs.review_minutes_per_alert
            * self.cost_inputs.cost_per_review_minute,
            2,
        )

    @property
    def decision_cost(self) -> float:
        return round(
            (self.intended_block_count * self.cost_inputs.cost_per_intended_block)
            + (self.alert_count * self.cost_inputs.cost_per_alert),
            2,
        )

    @property
    def total_cost(self) -> float:
        return round(self.decision_cost + self.review_cost, 2)
