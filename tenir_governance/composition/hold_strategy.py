# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Composition-level HOLD coordination for multi-TAU governance.

Per Blueprint v4.4 §0.8 and the composition simulator (v2.0, 9/9 tests):

    When multiple TAUs share resources, invariants, or a decision window,
    a HOLD in one TAU can affect the composition. The composition-level
    hold manager coordinates holds across TAUs using declared conflict
    resolution strategies.

Conflict resolution strategies (validated in simulator v2.0):
    Priority-Enforce  — highest-priority TAU passes; others HARD_VETO
    Serialize         — sequential allocation by priority until ceiling
    Pareto-Restore    — hold minimum TAUs to restore composition invariant
    Escalate          — HOLD global + escalation to Risk Officer
    Fail-Closed-Collectif — τ_K composition expired → lock entire composition

Default rule: composition invariant violated AND τ_K composition expired →
Fail-Closed Collectif. Equal priorities → C4 implicit → Fail-Closed Collectif.

Progressive unblock (Déblocage progressif post Fail-Closed):
    Per Blueprint v4.4 §8: listed as placeholder "à implémenter".
    Implemented here as a controlled re-admission protocol: after a
    Fail-Closed Collectif, TAUs are unblocked one at a time in priority
    order, with invariant verification before each unblock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompositionHoldStrategy(str, Enum):
    """Strategies for resolving composition-level holds.

    Per Blueprint v4.4 §0.8: conflict resolution strategies validated in
    the composition simulator v2.0 (9/9 tests passing).

    The strategy is declared in the CompositionManifest and determines
    how holds propagate across TAUs in the composition.
    """
    PRIORITY_ENFORCE = "Priority-Enforce"
    SERIALIZE = "Serialize"
    PARETO_RESTORE = "Pareto-Restore"
    ESCALATE = "Escalate"
    FAIL_CLOSED_COLLECTIF = "Fail-Closed-Collectif"


# Which strategies apply to which conflict types (per Blueprint v4.4)
_STRATEGY_CONFLICTS = {
    CompositionHoldStrategy.PRIORITY_ENFORCE: {"C1", "C2"},
    CompositionHoldStrategy.SERIALIZE: {"C1", "C2", "C3"},
    CompositionHoldStrategy.PARETO_RESTORE: {"C1"},
    CompositionHoldStrategy.ESCALATE: {"C1", "C2", "C3"},
    CompositionHoldStrategy.FAIL_CLOSED_COLLECTIF: {"C4", "tau_k_expired"},
}


@dataclass
class TauHoldState:
    """Per-TAU hold state within a composition.

    Tracks whether a TAU is held, why, and when it can be unblocked.
    """
    tau_id: str
    priority: int = 0  # lower value = higher priority (per §9.1 convention)
    is_held: bool = False
    hold_reason: str = ""
    hold_start: str = ""
    tau_k_seconds: float = 0.0
    composition_invariant_violated: bool = False


@dataclass
class CompositionHoldManager:
    """Manages composition-level HOLD coordination across multiple TAUs.

    Per Blueprint v4.4 §0.8: when TAUs share resources, invariants, or a
    decision window, holds must be coordinated. This manager implements
    the declared conflict resolution strategy.

    Usage:
        mgr = CompositionHoldManager(
            composition_id="liquidity-control-group",
            tau_k_composition_seconds=300,
        )
        mgr.register_tau("hedge-alpha-01", priority=1, tau_k_seconds=120)
        mgr.register_tau("hedge-beta-01", priority=2, tau_k_seconds=180)

        # When a TAU enters HOLD:
        mgr.enter_hold("hedge-alpha-01", reason="LCR breach imminent")

        # Resolve composition-level hold:
        result = mgr.resolve_composition_hold(
            strategy=CompositionHoldStrategy.PARETO_RESTORE,
            conflict_type="C1",
        )
    """
    composition_id: str = ""
    tau_k_composition_seconds: float = 0.0
    tau_k_composition_source: str = ""
    _taus: dict[str, TauHoldState] = field(default_factory=dict)
    _composition_hold_active: bool = False
    _composition_hold_reason: str = ""
    _composition_hold_start: str = ""
    _fail_closed: bool = False
    _progressive_unblock_enabled: bool = False
    _progressive_unblock_order: list[str] = field(default_factory=list)
    _progressive_unblock_index: int = 0

    def register_tau(
        self,
        tau_id: str,
        priority: int = 0,
        tau_k_seconds: float = 0.0,
    ) -> None:
        """Register a TAU in the composition."""
        self._taus[tau_id] = TauHoldState(
            tau_id=tau_id,
            priority=priority,
            tau_k_seconds=tau_k_seconds,
        )

    def enter_hold(
        self,
        tau_id: str,
        reason: str = "",
        composition_invariant_violated: bool = False,
    ) -> dict[str, Any]:
        """Mark a TAU as held within the composition.

        If the composition invariant is violated, this may trigger a
        composition-level hold depending on the strategy.
        """
        tau = self._taus.get(tau_id)
        if tau is None:
            return {"entered": False, "reason": f"TAU '{tau_id}' not registered."}

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        tau.is_held = True
        tau.hold_reason = reason
        tau.hold_start = now
        tau.composition_invariant_violated = composition_invariant_violated

        return {
            "entered": True,
            "tau_id": tau_id,
            "reason": reason,
            "composition_invariant_violated": composition_invariant_violated,
        }

    def resolve_composition_hold(
        self,
        strategy: CompositionHoldStrategy,
        conflict_type: str = "C1",
    ) -> dict[str, Any]:
        """Resolve a composition-level hold using the declared strategy.

        Per Blueprint v4.4: strategies are validated in the simulator v2.0.
        The strategy determines which TAUs are held, released, or vetoed.

        Returns a dict with:
            - strategy: the strategy used
            - held_taus: list of TAU IDs that are held
            - released_taus: list of TAU IDs that are released
            - vetoed_taus: list of TAU IDs that are HARD_VETOed
            - composition_hold: bool (whether composition-level hold is active)
            - reason: str
        """
        if conflict_type not in _STRATEGY_CONFLICTS.get(strategy, set()):
            return {
                "strategy": strategy.value,
                "held_taus": [],
                "released_taus": [],
                "vetoed_taus": [],
                "composition_hold": False,
                "reason": f"Strategy {strategy.value} does not apply to conflict type {conflict_type}.",
            }

        # Sort TAUs by priority (lower value = higher priority)
        sorted_taus = sorted(self._taus.values(), key=lambda t: t.priority)

        if strategy == CompositionHoldStrategy.PRIORITY_ENFORCE:
            # Highest-priority TAU passes; others HARD_VETO
            if not sorted_taus:
                return {"strategy": strategy.value, "held_taus": [],
                        "released_taus": [], "vetoed_taus": [],
                        "composition_hold": False, "reason": "No TAUs registered."}
            winner = sorted_taus[0]
            vetoed = [t.tau_id for t in sorted_taus[1:]]
            # Release the winner
            winner.is_held = False
            self._composition_hold_active = False
            return {
                "strategy": strategy.value,
                "held_taus": [],
                "released_taus": [winner.tau_id],
                "vetoed_taus": vetoed,
                "composition_hold": False,
                "reason": f"Priority-Enforce: {winner.tau_id} passes, {len(vetoed)} TAUs vetoed.",
            }

        elif strategy == CompositionHoldStrategy.SERIALIZE:
            # Sequential allocation by priority until ceiling
            # All TAUs are held except the first, which is released
            if not sorted_taus:
                return {"strategy": strategy.value, "held_taus": [],
                        "released_taus": [], "vetoed_taus": [],
                        "composition_hold": False, "reason": "No TAUs registered."}
            first = sorted_taus[0]
            held = [t.tau_id for t in sorted_taus[1:] if t.is_held]
            first.is_held = False
            self._composition_hold_active = len(held) > 0
            return {
                "strategy": strategy.value,
                "held_taus": held,
                "released_taus": [first.tau_id],
                "vetoed_taus": [],
                "composition_hold": len(held) > 0,
                "reason": f"Serialize: {first.tau_id} released, {len(held)} TAUs still held in queue.",
            }

        elif strategy == CompositionHoldStrategy.PARETO_RESTORE:
            # Hold minimum TAUs to restore composition invariant
            # The minimum set is the one(s) whose hold would restore the invariant
            # In practice: hold the lowest-priority TAUs that are violating
            held_taus = []
            released_taus = []
            for tau in sorted_taus:
                if tau.composition_invariant_violated:
                    tau.is_held = True
                    held_taus.append(tau.tau_id)
                else:
                    tau.is_held = False
                    released_taus.append(tau.tau_id)
            self._composition_hold_active = len(held_taus) > 0
            return {
                "strategy": strategy.value,
                "held_taus": held_taus,
                "released_taus": released_taus,
                "vetoed_taus": [],
                "composition_hold": len(held_taus) > 0,
                "reason": f"Pareto-Restore: {len(held_taus)} TAUs held, {len(released_taus)} released to restore invariant.",
            }

        elif strategy == CompositionHoldStrategy.ESCALATE:
            # HOLD global + escalation to Risk Officer
            held_taus = [t.tau_id for t in sorted_taus]
            for tau in sorted_taus:
                tau.is_held = True
            self._composition_hold_active = True
            self._composition_hold_reason = "Escalated to Risk Officer"
            return {
                "strategy": strategy.value,
                "held_taus": held_taus,
                "released_taus": [],
                "vetoed_taus": [],
                "composition_hold": True,
                "reason": "Escalate: all TAUs held, escalated to Risk Officer.",
            }

        elif strategy == CompositionHoldStrategy.FAIL_CLOSED_COLLECTIF:
            # Lock entire composition
            held_taus = [t.tau_id for t in sorted_taus]
            for tau in sorted_taus:
                tau.is_held = True
            self._composition_hold_active = True
            self._fail_closed = True
            self._composition_hold_reason = "Fail-Closed Collectif — composition invariant violated and τ_K expired"
            return {
                "strategy": strategy.value,
                "held_taus": held_taus,
                "released_taus": [],
                "vetoed_taus": [],
                "composition_hold": True,
                "fail_closed": True,
                "reason": "Fail-Closed Collectif: entire composition locked.",
            }

        return {
            "strategy": strategy.value,
            "held_taus": [],
            "released_taus": [],
            "vetoed_taus": [],
            "composition_hold": False,
            "reason": f"Unknown strategy: {strategy.value}",
        }

    def enter_fail_closed(self, reason: str = "") -> dict[str, Any]:
        """Enter Fail-Closed Collectif state — lock entire composition.

        Per Blueprint v4.4: default rule when composition invariant is
        violated and τ_K composition expired. Equal priorities → C4
        implicit → Fail-Closed Collectif.
        """
        self._fail_closed = True
        self._composition_hold_active = True
        self._composition_hold_reason = reason or "Fail-Closed Collectif"
        for tau in self._taus.values():
            tau.is_held = True
        return {
            "fail_closed": True,
            "held_taus": [t.tau_id for t in self._taus.values()],
            "reason": self._composition_hold_reason,
        }

    def enable_progressive_unblock(self) -> None:
        """Enable progressive unblock protocol.

        Per Blueprint v4.4 §8: "Déblocage progressif post Fail-Closed"
        was listed as placeholder "à implémenter". This implements it as
        a controlled re-admission protocol.

        TAUs are unblocked one at a time in priority order, with invariant
        verification before each unblock. The caller must call
        progressive_unblock_next() after verifying the invariant holds.
        """
        self._progressive_unblock_enabled = True
        self._progressive_unblock_order = sorted(
            self._taus.keys(),
            key=lambda tid: self._taus[tid].priority,
        )
        self._progressive_unblock_index = 0

    def progressive_unblock_next(
        self,
        invariant_verified: bool = True,
    ) -> dict[str, Any]:
        """Unblock the next TAU in priority order.

        Per Blueprint v4.4 §8: after Fail-Closed Collectif, TAUs are
        unblocked one at a time in priority order. Before each unblock,
        the composition invariant must be verified.

        Args:
            invariant_verified: whether the caller has verified that
                                unblocking this TAU will not violate
                                the composition invariant

        Returns:
            dict with unblocked (bool), tau_id, remaining, and reason
        """
        if not self._progressive_unblock_enabled:
            return {"unblocked": False, "reason": "Progressive unblock not enabled."}

        if self._progressive_unblock_index >= len(self._progressive_unblock_order):
            return {"unblocked": False, "reason": "All TAUs unblocked.",
                    "tau_id": None, "remaining": 0}

        if not invariant_verified:
            return {"unblocked": False, "reason": "Invariant not verified — cannot unblock.",
                    "tau_id": None,
                    "remaining": len(self._progressive_unblock_order) - self._progressive_unblock_index}

        tau_id = self._progressive_unblock_order[self._progressive_unblock_index]
        tau = self._taus[tau_id]
        tau.is_held = False
        self._progressive_unblock_index += 1

        remaining = len(self._progressive_unblock_order) - self._progressive_unblock_index

        if remaining == 0:
            # All TAUs unblocked — clear fail-closed
            self._fail_closed = False
            self._composition_hold_active = False

        return {
            "unblocked": True,
            "tau_id": tau_id,
            "remaining": remaining,
            "reason": f"Unblocked {tau_id}. {remaining} TAUs remaining.",
        }

    @property
    def is_fail_closed(self) -> bool:
        """Is the composition in Fail-Closed Collectif state?"""
        return self._fail_closed

    @property
    def composition_hold_active(self) -> bool:
        """Is a composition-level hold active?"""
        return self._composition_hold_active

    def get_held_taus(self) -> list[str]:
        """Get list of TAU IDs currently held."""
        return [t.tau_id for t in self._taus.values() if t.is_held]

    def get_tau_states(self) -> dict[str, dict]:
        """Get the state of all TAUs in the composition."""
        return {
            tau.tau_id: {
                "priority": tau.priority,
                "is_held": tau.is_held,
                "hold_reason": tau.hold_reason,
                "composition_invariant_violated": tau.composition_invariant_violated,
            }
            for tau in self._taus.values()
        }
