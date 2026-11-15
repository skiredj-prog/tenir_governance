"""RV-003 experimental DispatchProvider adapter.

External to the native TENIR-Gov repository.
Implements the native DispatchProvider Protocol using the existing
RV-003 receiver harness (EP-02 aligned).

Contract corrections (mandatory):
- HOLD never maps to FAILED.
- FAILED only on authoritative terminal non-execution (DENIED_REVOKED
  with durable evidence of no effect and no later commit possible).
- PENDING for accepted-but-unfinished work.
- UNKNOWN for ambiguous, timed-out, or contradictory outcomes.
- represented_user_sub from metadata is caller-supplied, not authenticated.
- No automatic re-dispatch on reconcile.
- CONFIRMED only with proof of durable receiver effect.

Frozen native reference: SHA 674c8606089251ea2f74890bd47bce3a717c3b91
No native sources are modified.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Native types (imported only after native_ref pin checks when wired)
# from tenir_governance.operations.provider import (
#     DispatchResult, ProviderOutcome, ReconciliationResult,
# )

# Local stand-in types for the adapter until full wiring.
# These mirror the native protocol exactly.
class ProviderOutcome:
    CONFIRMED = "confirmed"
    FAILED = "failed"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass
class DispatchResult:
    outcome: str
    provider_reference: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: float = 0.0
    safety_signals: list[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.outcome in (ProviderOutcome.CONFIRMED, ProviderOutcome.FAILED)


@dataclass
class ReconciliationResult:
    resolved: bool
    outcome: str = ProviderOutcome.UNKNOWN
    provider_reference: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    safety_signals: list[str] = field(default_factory=list)


class RV003DispatchProvider:
    """Experimental DispatchProvider that talks to the existing RV-003 receiver.

    Transport is injectable so the same class can be used against:
    - in-process receiver control (lab)
    - ZeroMQ endpoint of a running receiver process
    - future MCP adapter
    """

    def __init__(self, receiver_control, run_id: str | None = None):
        """
        Args:
            receiver_control: callable(cmd_dict) -> response_dict
                              (matches the control(m) interface of receiver_p.py)
            run_id: optional run identifier for traces
        """
        self.receiver_control = receiver_control
        self.run_id = run_id or f"rv003-native-lab-{uuid.uuid4().hex[:12]}"
        self._seen_keys: dict[str, dict] = {}  # idempotency_key -> binding snapshot

    def execute(
        self,
        idempotency_key: str,
        action_type: str,
        amount: float,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        t0 = time.monotonic()
        metadata = metadata or {}
        safety: list[str] = []

        # Identity provenance: metadata is caller-supplied, not authenticated.
        represented = metadata.get("represented_user_sub")
        if represented:
            safety.append("represented_user_sub_from_metadata_unverified")

        # Stable idempotency: conflicting resubmission is rejected,
        # but does NOT alter or fail the original operation.
        if idempotency_key in self._seen_keys:
            prev = self._seen_keys[idempotency_key]
            if (prev.get("action_type") != action_type
                    or prev.get("agent_id") != agent_id
                    or prev.get("payload_digest") != metadata.get("payload_digest")):
                return DispatchResult(
                    outcome=ProviderOutcome.UNKNOWN,  # never FAILED for the original
                    error="REQUEST_CONFLICT: different payload or identity; original operation unchanged",
                    safety_signals=["request_conflict_original_preserved"],
                    latency_ms=(time.monotonic() - t0) * 1000,
                    raw_response={"conflict": True, "original_preserved": True},
                )

        # Build receiver binding (delegates to existing rv003_binding when wired).
        # For the skeleton we record the intent.
        binding_snapshot = {
            "idempotency_key": idempotency_key,
            "action_type": action_type,
            "agent_id": agent_id,
            "amount": amount,
            "payload_digest": metadata.get("payload_digest"),
            "represented_user_sub": represented,
            "metadata_keys": sorted(metadata.keys()),
        }
        self._seen_keys[idempotency_key] = binding_snapshot

        # Call receiver ACCEPT (paused).
        # Real wiring uses the existing accept() path of receiver_p.py.
        accept_resp = self.receiver_control({
            "cmd": "ACCEPT",
            "idempotency_key": idempotency_key,
            "action_type": action_type,
            "agent_id": agent_id,
            "amount": amount,
            "metadata": metadata,
            "run_id": self.run_id,
        })

        state = accept_resp.get("state") or accept_resp.get("status")
        latency = (time.monotonic() - t0) * 1000

        # Mapping (corrected)
        if state in ("ACCEPTED_PAUSED", "ACCEPTED", "QUEUED"):
            return DispatchResult(
                outcome=ProviderOutcome.PENDING,
                provider_reference=accept_resp.get("acceptance_id") or idempotency_key,
                raw_response=accept_resp,
                latency_ms=latency,
                safety_signals=safety + ["accepted_not_confirmed"],
            )

        if state == "DENIED_REVOKED":
            # Only map to FAILED if authoritative terminal evidence is present.
            if accept_resp.get("terminal_no_effect") and accept_resp.get("no_replay_possible"):
                return DispatchResult(
                    outcome=ProviderOutcome.FAILED,
                    provider_reference=idempotency_key,
                    raw_response=accept_resp,
                    latency_ms=latency,
                    safety_signals=safety + ["terminal_denial_authoritative"],
                )
            return DispatchResult(
                outcome=ProviderOutcome.UNKNOWN,
                error="DENIED_REVOKED without terminal evidence",
                raw_response=accept_resp,
                latency_ms=latency,
                safety_signals=safety + ["denial_not_yet_terminal"],
            )

        # HOLD and everything else stay non-FAILED.
        return DispatchResult(
            outcome=ProviderOutcome.UNKNOWN,
            error=f"unresolved state: {state}",
            raw_response=accept_resp,
            latency_ms=latency,
            safety_signals=safety + ["hold_or_ambiguous_not_failed"],
        )

    def reconcile(
        self,
        idempotency_key: str,
        provider_reference: str = "",
    ) -> ReconciliationResult:
        """Query durable receiver state. No re-dispatch."""
        status = self.receiver_control({
            "cmd": "QUERY_STATUS",
            "operation_id": idempotency_key,
            "provider_reference": provider_reference,
        })

        state = status.get("state")
        effect_count = status.get("effect_count", 0)
        terminal = status.get("terminal", False)
        no_replay = status.get("no_replay_possible", False)

        # Durable COMMITTED from the receiver is authoritative evidence of the effect
        if state == "COMMITTED":
            return ReconciliationResult(
                resolved=True,
                outcome=ProviderOutcome.CONFIRMED,
                provider_reference=provider_reference or idempotency_key,
                raw_response=status,
                safety_signals=["durable_effect_verified"],
            )

        if (state == "DENIED_REVOKED"
                and effect_count == 0
                and terminal
                and no_replay):
            return ReconciliationResult(
                resolved=True,
                outcome=ProviderOutcome.FAILED,
                provider_reference=provider_reference or idempotency_key,
                raw_response=status,
                safety_signals=["terminal_non_execution_authoritative"],
            )

        if state in ("ACCEPTED_PAUSED", "COMMIT_CHECKING", "HOLD_AUTHORITY_UNKNOWN", "HOLD_AUTHORITY_CHANGED"):
            return ReconciliationResult(
                resolved=False,
                outcome=ProviderOutcome.PENDING,
                raw_response=status,
                safety_signals=["still_open_or_hold"],
            )

        return ReconciliationResult(
            resolved=False,
            outcome=ProviderOutcome.UNKNOWN,
            raw_response=status,
            error="ambiguous or incomplete evidence",
            safety_signals=["ambiguous_state"],
        )


def make_lab_provider(receiver_control, run_id: str | None = None) -> RV003DispatchProvider:
    """Factory for the experimental adapter."""
    return RV003DispatchProvider(receiver_control, run_id)
