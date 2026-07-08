from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .control_auth import OperatorRegistry, TransitionProof
from .ledger import HashChainedLedger
from .models import EventSample, OperatingMode, PolicyBundle, Verdict
from .trajectory import TrajectoryTracker


class TenirMonitor:
    """Internal v4 test monitor.

    Shadow by default. Human escalation is explicit, ledgered, and replayable.
    """

    def __init__(
        self,
        policy: Optional[PolicyBundle] = None,
        ledger_path: str | Path = "audit/governance_ledger.jsonl",
        mode: Optional[OperatingMode | str] = None,
        replay_history: bool = True,
        operator_registry: Optional[OperatorRegistry] = None,
    ) -> None:
        self.policy = policy or PolicyBundle()
        self.policy.validate()
        self.tracker = TrajectoryTracker(self.policy)
        self.ledger = HashChainedLedger(ledger_path)
        self.operator_registry = operator_registry or OperatorRegistry()
        self.mode = OperatingMode.SHADOW_PASSIVE
        self._used_control_nonces: set[str] = set()

        if replay_history:
            self._replay_state_from_ledger()
        if mode is not None:
            self.mode = OperatingMode.parse(mode)

    def _replay_state_from_ledger(self) -> None:
        recovered_mode = OperatingMode.SHADOW_PASSIVE
        for entry in self.ledger.iter_entries():
            payload = entry.get("payload", {})
            entry_type = payload.get("type")
            if entry_type == "observation":
                payload_mode = payload.get("mode")
                if payload_mode is not None:
                    try:
                        recovered_mode = OperatingMode.parse(str(payload_mode))
                    except (TypeError, ValueError) as exc:
                        raise ValueError("unrecognized observation mode in ledger") from exc
                event_payload = payload.get("event")
                if not isinstance(event_payload, dict):
                    raise ValueError("observation entry missing event payload")
                self.tracker.update(EventSample(**event_payload))
            elif entry_type == "control_transition":
                target_mode = payload.get("to_mode")
                transition = payload.get("transition")
                control_proof = payload.get("control_proof")
                if isinstance(control_proof, dict):
                    nonce = control_proof.get("nonce")
                    if isinstance(nonce, str) and nonce.strip():
                        self._used_control_nonces.add(nonce)
                if target_mode is not None:
                    try:
                        recovered_mode = OperatingMode.parse(str(target_mode))
                    except (TypeError, ValueError) as exc:
                        raise ValueError("unrecognized control transition in ledger") from exc
                elif transition == "SHADOW_TO_ENFORCE":
                    recovered_mode = OperatingMode.ENFORCE
                elif transition == "ENFORCE_TO_SHADOW":
                    recovered_mode = OperatingMode.SHADOW_PASSIVE
                else:
                    raise ValueError("unrecognized control transition in ledger")
            elif entry_type is None:
                raise ValueError("ledger entry missing type")
        self.mode = recovered_mode

    def _log_denied_transition(
        self,
        *,
        expected_action: str,
        proof: TransitionProof,
        failure_reason: str,
    ) -> str:
        payload = {
            "type": "unauthorized_transition_attempt",
            "requested_transition": expected_action,
            "failure_reason": failure_reason,
            "policy_version": self.policy.version,
            "control_proof": asdict(proof),
        }
        return self.ledger.append(payload)

    def _authorize_transition(
        self,
        *,
        expected_action: str,
        proof: TransitionProof,
    ) -> None:
        if proof.action != expected_action:
            self._log_denied_transition(
                expected_action=expected_action,
                proof=proof,
                failure_reason="proof action does not match requested transition",
            )
            raise PermissionError("proof action does not match requested transition")

        if proof.nonce in self._used_control_nonces:
            self._log_denied_transition(
                expected_action=expected_action,
                proof=proof,
                failure_reason="control proof nonce has already been used",
            )
            raise PermissionError("control proof nonce has already been used")

        is_valid, detail = self.operator_registry.verify_transition_proof(proof)
        if not is_valid:
            self._log_denied_transition(
                expected_action=expected_action,
                proof=proof,
                failure_reason=detail,
            )
            raise PermissionError(detail)

        self._used_control_nonces.add(proof.nonce)

    def _action_for_trajectory(self, *, alert: bool, intended_block: bool) -> str:
        if self.mode == OperatingMode.SHADOW_OFF:
            return "allow"
        if self.mode == OperatingMode.SHADOW_PASSIVE:
            if intended_block:
                return "allow_with_intended_block"
            if alert:
                return "allow_with_alert"
            return "allow"
        if self.mode == OperatingMode.SHADOW_CRITICAL:
            if intended_block:
                return "block"
            if alert:
                return "allow_with_alert"
            return "allow"
        if intended_block:
            return "block"
        if alert:
            return "block_on_alert"
        return "allow"

    def observe(self, event: EventSample) -> Verdict:
        trajectory = self.tracker.update(event)

        # R4 local action mapping (preserves backward-compat vocabulary)
        action = self._action_for_trajectory(
            alert=trajectory.alert,
            intended_block=trajectory.intended_block,
        )

        # ── Sprint 12: additive wiring to shared PolicyEngine ────────────────
        # The shared engine's decision and rationale are recorded in the
        # ledger alongside the R4 action. R4 action vocabulary is preserved
        # (backward-compat); shared engine adds auditability by design.
        shared_decision = None
        shared_rationale = None
        policy_version = self.policy.version
        try:
            engine = self.policy.to_policy_engine()
            policy_version = engine.version
            shared_decision, shared_rationale, _, _ = engine.evaluate_membrane(
                s_score=trajectory.stability,
                ds_de=trajectory.ds_de,
                d2s_de2=trajectory.d2s_de2,
                option_space=event.option_space,
                projected_events_to_zero=trajectory.projected_events_to_zero,
                operating_mode=self.mode.value,
            )
        except ImportError:
            pass
        # ──────────────────────────────────────────────────────────────────────

        payload = {
            "type": "observation",
            "mode": self.mode.value,
            "action": action,
            "policy_version": policy_version,
            "event": asdict(event),
            "trajectory": asdict(trajectory),
        }
        if shared_decision:
            payload["shared_engine_decision"] = shared_decision
        if shared_rationale:
            payload["rationale"] = shared_rationale
        chain_hash = self.ledger.append(payload)

        return Verdict(
            mode=self.mode,
            action=action,
            trajectory=trajectory,
            observed_at=event.observed_at,
            ledger_path=str(self.ledger.path),
            chain_hash=chain_hash,
        )

    def close_the_glass(self, proof: TransitionProof) -> str:
        self._authorize_transition(
            expected_action="SHADOW_TO_ENFORCE",
            proof=proof,
        )
        previous_mode = self.mode
        self.mode = OperatingMode.ENFORCE
        payload = {
            "type": "control_transition",
            "transition": "SHADOW_TO_ENFORCE",
            "from_mode": previous_mode.value,
            "to_mode": self.mode.value,
            "operator_id": proof.operator_id,
            "reason": proof.reason,
            "policy_version": self.policy.version,
            "control_proof": asdict(proof),
        }
        return self.ledger.append(payload)

    def reopen_to_shadow(self, proof: TransitionProof) -> str:
        self._authorize_transition(
            expected_action="ENFORCE_TO_SHADOW",
            proof=proof,
        )
        previous_mode = self.mode
        self.mode = OperatingMode.SHADOW_PASSIVE
        payload = {
            "type": "control_transition",
            "transition": "ENFORCE_TO_SHADOW",
            "from_mode": previous_mode.value,
            "to_mode": self.mode.value,
            "operator_id": proof.operator_id,
            "reason": proof.reason,
            "policy_version": self.policy.version,
            "control_proof": asdict(proof),
        }
        return self.ledger.append(payload)
