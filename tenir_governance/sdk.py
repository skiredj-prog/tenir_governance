"""
TENIR SDK — Governance Branch Public Interface
===============================================
SPRINT 5 — SDK/Governance Branch

The SDK is the clean public interface over the governance runtime.
Both R4 (TenirMonitor) and R5 (r5_server.py) wire through here.

Philosophy (from TENIR doctrine):
  The SDK surface is the "tenir>" — the lawful passage.
  It enforces three things:
    1. Policy is validated before any evaluation begins.
    2. Every evaluation is ledgered (auditability by design).
    3. The membrane decision is produced by PolicyEngine — never ad-hoc.

Usage (R4 migration path):
    from tenir_governance.sdk import TENIRGovernanceClient, GovernanceEvent

    client = TENIRGovernanceClient(policy=PolicyEngine.default())
    event = GovernanceEvent(pressure=0.8, velocity=0.7, capacity=1.0, option_space=0.5)
    result = client.adjudicate(event)
    print(result.decision)           # "allow_with_alert"
    print(result.rationale)          # plain-language explanation
    print(result.ces_state)          # "TENSION"
    print(result.business_label)     # "Under Structural Stress — Alert"

Usage (R5 integration):
    # r5_server.py adjudication endpoint calls:
    result = client.adjudicate_from_nsl(nsl_result)

Usage (Business demo):
    result = client.adjudicate(event)
    dashboard_payload = result.to_business_payload()
    # Returns human-readable dict for the Copilot / Lens surfaces

Usage (CLI / batch):
    python -m tenir_governance.sdk --events sample_events.json --policy default
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .nomenclature import (
    CESStateNames, KernelFieldNames, MembraneDecisionNames,
    NSLFieldNames, OperatingModeNames, LedgerEntryTypes,
)
from .policy_engine import PolicyEngine, PolicyViolation
from .validator import TENIRValidator


# ─── DATA TYPES ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GovernanceEvent:
    """
    Normalized governance event — the SDK's public input contract.
    Replaces EventSample (R4) and the raw dict inputs in R5.
    """
    pressure: float
    velocity: float
    capacity: float
    option_space: Optional[float] = None
    workflow_id: Optional[str] = None
    workflow_type: str = "rnd"
    actor_ref: str = "operator"
    source_ref: str = "sdk"
    nsl_input: Optional[str] = None       # if event originated from NSL
    nsl_backend: Optional[str] = None     # "llm" | "grammar" | None
    nsl_confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class GovernanceResult:
    """
    The SDK's public output contract.
    Carries the full adjudication result in both kernel and business terms.
    """
    event_id: str
    observed_at: str

    # Kernel outputs
    s_score: float
    ds_de: float
    d2s_de2: float
    projected_events_to_zero: Optional[int]

    # Adjudication outputs
    decision: str              # MembraneDecisionNames
    rationale: str
    alert: bool
    intended_block: bool
    ces_state: str             # CESStateNames
    operating_mode: str        # OperatingModeNames
    policy_version: str

    # Cryptographic provenance
    chain_hash: str
    entry_index: int

    # Business layer
    @property
    def business_label(self) -> str:
        return CESStateNames.BUSINESS_ALIAS.get(self.ces_state, self.ces_state)

    @property
    def decision_label(self) -> str:
        return MembraneDecisionNames.BUSINESS_ALIAS.get(self.decision, self.decision)

    @property
    def is_blocked(self) -> bool:
        return self.decision == MembraneDecisionNames.BLOCK

    @property
    def recommended_posture(self) -> str:
        return CESStateNames.RECOMMENDED_POSTURE.get(self.ces_state, "SHADOW_PASSIVE")

    def to_business_payload(self) -> Dict:
        """Human-readable payload for Copilot / Lens UI surfaces."""
        return {
            "event_id":          self.event_id,
            "timestamp":         self.observed_at,
            "stability_index":   f"{self.s_score:.3f}",
            "stability_label":   KernelFieldNames.BUSINESS_ALIAS["stability"],
            "institutional_state": self.business_label,
            "governance_action":   self.decision_label,
            "advisory":          self.alert,
            "governance_block":  self.intended_block,
            "hard_block":        self.is_blocked,
            "recommended_posture": self.recommended_posture,
            "rationale":         self.rationale,
            "policy_version":    self.policy_version,
            "chain_hash":        self.chain_hash[:12] + "…",
            "entry_number":      self.entry_index,
        }

    def to_r5_frame(self) -> Dict:
        """Emit as R5 WebSocket TRAJECTORY_UPDATE frame payload."""
        return {
            "s_score":            self.s_score,
            "ds_de":              self.ds_de,
            "d2s_de2":            self.d2s_de2,
            "horizon_events":     self.projected_events_to_zero,
            "ces_state":          self.ces_state,
            "operating_mode":     self.operating_mode,
            "membrane_decision":  self.decision,
        }


# ─── SDK CLIENT ───────────────────────────────────────────────────────────────

class TENIRGovernanceClient:
    """
    SDK client for the TENIR governance runtime.

    Explicit dependencies (Sprint 1 requirement):
      - PolicyEngine: must be injected; no local threshold literals
      - TENIRValidator: run on init; governance cannot proceed if validation fails
      - In-memory ledger (can be swapped for HashChainedLedger or DistributedLedger)
    """

    def __init__(
        self,
        policy: Optional[PolicyEngine] = None,
        operating_mode: str = OperatingModeNames.SHADOW_PASSIVE,
        ledger_path: Optional[Path] = None,
        validate_on_init: bool = True,
    ) -> None:
        # ── Explicit policy dependency ────────────────────────────────────────
        self.policy = policy or PolicyEngine.default()

        # ── Validator gate ────────────────────────────────────────────────────
        if validate_on_init:
            validator = TENIRValidator(policy=self.policy)
            report = validator.validate_all()
            if not report.passed:
                raise PolicyViolation(
                    f"Governance client cannot initialize: validator failed.\n{report.summary()}"
                )

        self.operating_mode = operating_mode
        self._stability_history: List[float] = []
        self._entry_count = 0
        self._last_hash = "GENESIS"
        self._ledger_path = ledger_path
        self._in_memory_log: List[Dict] = []

        if ledger_path:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)


    @classmethod
    def for_demo(cls) -> "TENIRGovernanceClient":
        """Low-validation client for demo/testing (validator still runs)."""
        return cls(policy=PolicyEngine.default())

    # ── Core adjudication ─────────────────────────────────────────────────────

    def adjudicate(self, event: GovernanceEvent) -> GovernanceResult:
        """
        Full governance adjudication.

        Pipeline:
          1. Kernel math (S = K / (P·V + ε))
          2. Trajectory derivatives (dS/de, d²S/de²)
          3. PolicyEngine.evaluate_membrane() — no local thresholds
          4. PolicyEngine.classify_ces()
          5. Ledger append
          6. Return GovernanceResult
        """
        # 1. Kernel math
        s = self.policy.capacity_s(event.pressure, event.velocity, event.capacity)

        # 2. Trajectory derivatives
        self._stability_history.append(s)
        ds_de = 0.0
        d2s_de2 = 0.0
        if len(self._stability_history) >= 2:
            ds_de = self._stability_history[-1] - self._stability_history[-2]
        if len(self._stability_history) >= 3:
            prev_ds = self._stability_history[-2] - self._stability_history[-3]
            d2s_de2 = ds_de - prev_ds

        # Keep history bounded
        if len(self._stability_history) > self.policy.event_window:
            self._stability_history.pop(0)

        # Horizon
        projected: Optional[int] = None
        if ds_de < 0:
            try:
                from math import ceil
                projected = max(0, ceil(s / abs(ds_de)))
            except ZeroDivisionError:
                pass

        # 3. PolicyEngine membrane — EXPLICIT dependency
        decision, rationale, alert, intended_block = self.policy.evaluate_membrane(
            s_score=s,
            ds_de=ds_de,
            d2s_de2=d2s_de2,
            option_space=event.option_space,
            projected_events_to_zero=projected,
            operating_mode=self.operating_mode,
        )

        # 4. CES classification — EXPLICIT dependency
        ces_state = self.policy.classify_ces(s, ds_de, event.option_space)

        # 5. Ledger
        event_id = event.workflow_id or str(uuid4())
        chain_hash = self._ledger_append(event, s, ds_de, d2s_de2, decision, rationale, ces_state)
        self._entry_count += 1

        return GovernanceResult(
            event_id=event_id,
            observed_at=event.observed_at,
            s_score=round(s, 6),
            ds_de=round(ds_de, 6),
            d2s_de2=round(d2s_de2, 6),
            projected_events_to_zero=projected,
            decision=decision,
            rationale=rationale,
            alert=alert,
            intended_block=intended_block,
            ces_state=ces_state,
            operating_mode=self.operating_mode,
            policy_version=self.policy.version,
            chain_hash=chain_hash,
            entry_index=self._entry_count,
        )

    def adjudicate_from_nsl(self, nsl_result: Dict) -> GovernanceResult:
        """
        Adjudicate from an R5 NSL inference result dict.
        Bridges R5 neuro-symbolic output → SDK governance contract.
        """
        params = nsl_result.get("params", {})
        event = GovernanceEvent(
            pressure=params.get("pressure", 0.5),
            velocity=params.get("velocity", 0.5),
            capacity=params.get("capacity", 1.0),
            option_space=params.get("option_space"),
            workflow_type=nsl_result.get("entity_type", "rnd").lower(),
            nsl_input=nsl_result.get("raw_input"),
            nsl_backend=nsl_result.get("backend"),
            nsl_confidence=nsl_result.get("confidence"),
            metadata={"nsl_intent": nsl_result.get("intent")},
        )
        return self.adjudicate(event)

    def transition_mode(self, new_mode: str, operator_id: str, reason: str) -> None:
        """
        Governed mode transition. Ledgers the intent.
        Full ceremony (oath + HMAC) is handled by control_auth in R4
        or KeyCeremony in R5 — this SDK records the transition outcome.
        """
        old_mode = self.operating_mode
        self.operating_mode = new_mode
        self._ledger_append_transition(old_mode, new_mode, operator_id, reason)

    # ── Ledger ────────────────────────────────────────────────────────────────

    def _ledger_append(
        self, event: GovernanceEvent, s: float, ds_de: float,
        d2s_de2: float, decision: str, rationale: str, ces_state: str,
    ) -> str:
        payload = {
            "type": LedgerEntryTypes.OBSERVATION,
            "event_id": event.workflow_id or "anon",
            "workflow_type": event.workflow_type,
            "operating_mode": self.operating_mode,
            "policy_version": self.policy.version,
            "s_score": round(s, 6),
            "ds_de": round(ds_de, 6),
            "d2s_de2": round(d2s_de2, 6),
            "decision": decision,
            "ces_state": ces_state,
            "rationale": rationale,
            "nsl_backend": event.nsl_backend,
            "nsl_confidence": event.nsl_confidence,
        }
        return self._append_to_chain(payload)

    def _ledger_append_transition(
        self, from_mode: str, to_mode: str, operator_id: str, reason: str
    ) -> str:
        payload = {
            "type": LedgerEntryTypes.CONTROL_TRANSITION,
            "from_mode": from_mode,
            "to_mode": to_mode,
            "operator_id": operator_id,
            "reason": reason,
            "policy_version": self.policy.version,
        }
        return self._append_to_chain(payload)

    def _append_to_chain(self, payload: Dict) -> str:
        clean = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        chain_input = f"{self._last_hash}|{clean}".encode()
        chain_hash = hashlib.sha256(chain_input).hexdigest()

        entry = {
            "previous_hash": self._last_hash,
            "chain_hash": chain_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        self._last_hash = chain_hash
        self._in_memory_log.append(entry)

        if self._ledger_path:
            with self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

        return chain_hash

    # ── State inspection ──────────────────────────────────────────────────────

    def current_state(self) -> Dict:
        """Current system state snapshot — for polling UIs."""
        s = self._stability_history[-1] if self._stability_history else None
        return {
            "mode": self.operating_mode,
            "mode_label": OperatingModeNames.BUSINESS_ALIAS.get(self.operating_mode),
            "entry_count": self._entry_count,
            "current_s": round(s, 4) if s else None,
            "chain_hash": self._last_hash[:16] + "…",
            "policy_version": self.policy.version,
            "policy_fingerprint": self.policy.fingerprint(),
        }

    def validate_chain(self) -> bool:
        """In-memory chain integrity check."""
        prev = "GENESIS"
        for entry in self._in_memory_log:
            if entry.get("previous_hash") != prev:
                return False
            prev = entry.get("chain_hash", "")
        return True


# ─── CLI ENTRY POINT ──────────────────────────────────────────────────────────

def main() -> None:
    import argparse, sys
    parser = argparse.ArgumentParser(description="TENIR Governance SDK CLI")
    parser.add_argument("--policy", choices=["default"], default="default")
    parser.add_argument("--mode", default="shadow-passive")
    parser.add_argument("--events", help="Path to JSON events file")
    parser.add_argument("--pressure", type=float)
    parser.add_argument("--velocity", type=float)
    parser.add_argument("--capacity", type=float)
    parser.add_argument("--option-space", type=float)
    parser.add_argument("--business", action="store_true", help="Show business-layer output")
    args = parser.parse_args()

    policy_map = {"default": PolicyEngine.default}
    client = TENIRGovernanceClient(policy=policy_map[args.policy](), operating_mode=args.mode.upper().replace("-","_"))

    events_to_run = []
    if args.events:
        with open(args.events) as f:
            events_to_run = json.load(f)
    elif args.pressure is not None:
        events_to_run = [{"pressure": args.pressure, "velocity": args.velocity or 0.5,
                           "capacity": args.capacity or 1.0, "option_space": args.option_space}]

    for ev_dict in events_to_run:
        ev = GovernanceEvent(**{k: v for k, v in ev_dict.items()
                                if k in GovernanceEvent.__dataclass_fields__})
        result = client.adjudicate(ev)
        if args.business:
            print(json.dumps(result.to_business_payload(), indent=2))
        else:
            print(f"[{result.entry_index}] S={result.s_score:.4f} | {result.ces_state} | {result.decision} | {result.rationale[:60]}")


if __name__ == "__main__":
    main()
