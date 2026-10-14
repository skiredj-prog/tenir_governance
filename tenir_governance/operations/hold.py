# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""HOLD lifecycle manager — governed state for suspended operations.

Per doctrine v7.1 §6 and Blueprint v4.4:

    HOLD is a first-class protocol state — a designed suspension of agency
    under pressure, not a system failure. The operation is not denied; it
    is pending evidence or review.

HOLD vs QUARANTINE_LOCK (v7.1 §6):
    HOLD withholds admission for the specific decision at issue while the
    evidence or governance question remains unresolved. It is a disposition,
    not automatically a persistent LEI-level quarantine.

    QUARANTINE_LOCK blocks ALL new attempt admission and execution dispatch
    for the LEI until an authorized release or resolution transition succeeds.

HOLD exit paths (from the doctrine):
    1. Evidence becomes sufficient → re-adjudicate → RESERVED (PASS/FLAG)
    2. τ_K expires (unresolved HOLD) → FAIL-CLOSED → FAILED
    3. Escalation → ESCALATED (absorbing until authorized exit)
    4. Authorized exit from ESCALATED → RESERVED (with ExitReceipt)
    5. Authorized exit from ESCALATED → FAILED (with ExitReceipt, if denied)

    Time-based exit from ESCALATED is REJECTED: τ_K expiry produces UNKNOWN,
    not resolution (v7.1 §19).

ESCALATED is absorbing:
    It remains until an authorization model defines who signs, under what
    scope, and with which receipt (v7.1 §19). No automatic time-based exit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .registry import (
    DurableOperationRegistry,
    OperationRecord,
    OperationState,
    HoldRecord,
    ExitReceipt,
    _TransitionEvidence,
)


def _utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _utc_now_seconds() -> float:
    """Return current UTC time as epoch seconds (for τ_K calculations)."""
    return datetime.now(timezone.utc).timestamp()


class HoldManager:
    """Manages the HOLD lifecycle for operations in the durable registry.

    This is NOT a replacement for the governor's adjudicate() — it manages
    the persistent HOLD state that follows a HOLD decision. The governor
    produces the decision; the HoldManager manages the governed lifecycle.

    Per KB3: implementation states must not be rendered as doctrine. This
    class holds implementation-level governance enforcement authority only.
    """

    def __init__(self, registry: DurableOperationRegistry):
        self._registry = registry
        self._registry._register_lifecycle_issuer(self)

    def enter_hold(
        self,
        idempotency_key: str,
        tau_k_seconds: float = 0.0,
        tau_k_source: str = "",
    ) -> dict[str, Any]:
        """Transition an operation from CREATED to HOLD.

        Creates a HoldRecord with the temporal validity bound τ_K.

        No generic τ_K default is imposed. Scenario callers may explicitly
        pass 3600 seconds (1 hour) for a meaningful demonstration horizon.
        Production callers should pass an organization-owned SLA explicitly.

        Returns a dict with:
            - entered: bool (whether the transition succeeded)
            - operation_state: str (current state)
            - hold_record: dict | None (hold metadata if entered)
            - reason: str (explanation if not entered)
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {
                "entered": False,
                "operation_state": None,
                "hold_record": None,
                "reason": f"Operation '{idempotency_key}' not found.",
            }

        if record.state != OperationState.CREATED:
            return {
                "entered": False,
                "operation_state": record.state.value,
                "hold_record": record.hold_record.to_dict() if record.hold_record else None,
                "reason": f"Operation is in state {record.state.value}, not CREATED.",
            }

        hold_record = HoldRecord(
            hold_start=_utc_now(),
            tau_k_seconds=tau_k_seconds,
            tau_k_source=tau_k_source,
        )
        record.hold_record = hold_record

        success, _prev = self._registry.transition_to(
            idempotency_key, OperationState.HOLD,
        )
        if not success:
            return {
                "entered": False,
                "operation_state": record.state.value,
                "hold_record": None,
                "reason": "Transition CREATED → HOLD failed.",
            }

        return {
            "entered": True,
            "operation_state": OperationState.HOLD.value,
            "hold_record": hold_record.to_dict(),
            "reason": "Operation entered HOLD state.",
        }

    def resolve_hold(self, idempotency_key: str) -> dict[str, Any]:
        """Reject direct HOLD resolution outside the governor's re-adjudication path.

        A HoldManager-only call cannot prove that fresh scoring and membrane
        adjudication produced PASS or FLAG. Call Governor.resolve_hold() with
        the passage and updated evidence instead.
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {
                "resolved": False,
                "operation_state": None,
                "reason": f"Operation '{idempotency_key}' not found.",
            }

        state = record.state.value
        if record.state != OperationState.HOLD:
            return {
                "resolved": False,
                "operation_state": state,
                "reason": f"Operation is in state {state}, not HOLD.",
            }

        return {
            "resolved": False,
            "operation_state": state,
            "reason": (
                "Direct HoldManager resolution is disabled. Use "
                "Governor.resolve_hold() with a passage and updated evidence "
                "so fresh adjudication is enforced."
            ),
        }

    def escalate_hold(
        self,
        idempotency_key: str,
        escalated_by: str = "",
    ) -> dict[str, Any]:
        """Transition HOLD → ESCALATED.

        Per v7.1 §19: ESCALATED has no time-based resolution. It remains
        pending authorization; if τ_K expires, expire_escalated() transitions
        it to UNKNOWN, not to a resolved or executable state.

        Per Blueprint v4.4: Escalate strategy = HOLD global + escalation
        to Risk Officer.

        Returns a dict with:
            - escalated: bool
            - operation_state: str
            - reason: str
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {"escalated": False, "operation_state": None,
                    "reason": f"Operation '{idempotency_key}' not found."}

        if record.state != OperationState.HOLD:
            return {"escalated": False, "operation_state": record.state.value,
                    "reason": f"Operation is in state {record.state.value}, not HOLD."}
        if record.hold_record is None:
            return {"escalated": False, "operation_state": record.state.value,
                    "reason": "Cannot escalate without an initialized HoldRecord."}
        if not isinstance(escalated_by, str):
            return {
                "escalated": False,
                "operation_state": record.state.value,
                "reason": "Escalation metadata escalated_by must be a string.",
            }

        auth_class = "HOLD_ESCALATION"
        evidence = _TransitionEvidence(
            idempotency_key, OperationState.HOLD, OperationState.ESCALATED,
            auth_class, "hold_escalation", (str(escalated_by),),
        )
        try:
            capability = self._registry._issue_transition_authorization(
                idempotency_key, OperationState.HOLD, OperationState.ESCALATED,
                auth_class, evidence, issuer=self,
            )
        except (RuntimeError, PermissionError, ValueError) as exc:
            current = self._registry.get(idempotency_key)
            return {"escalated": False,
                    "operation_state": current.state.value if current else None,
                    "reason": f"Transition authorization rejected: {exc}"}
        success, _prev = self._registry.transition_to(
            idempotency_key, OperationState.ESCALATED, authorization=capability,
            hold_record_updates={
                "escalated_at": _utc_now(),
                "escalated_by": escalated_by,
                "exit_reason": "escalated",
            },
        )
        return {
            "escalated": success,
            "operation_state": self._registry.get(idempotency_key).state.value if self._registry.get(idempotency_key) else None,
            "reason": "Hold escalated to Risk Officer — ESCALATED is absorbing." if success
                      else "Transition HOLD → ESCALATED failed.",
        }

    def authorize_exit(
        self,
        idempotency_key: str,
        authorized_by: str,
        scope: str,
        authorization: str,
        reason: str = "",
        approve: bool = True,
    ) -> dict[str, Any]:
        """Authorized exit from ESCALATED state.

        Per v7.1 §19: ESCALATED remains absorbing until an authorization model
        defines who signs, under what scope, and with which receipt.

        Per Blueprint v4.4 §5: overrides constitutionnels uniquement via
        Ceremony G-R5-001 (authentification opérateur).

        Args:
            authorized_by: who is authorizing the exit (operator ID)
            scope: scope of the authorization
            authorization: authorization mechanism (e.g., "ceremony-G-R5-001")
            reason: why the exit is being authorized
            approve: True → transition to RESERVED (proceed);
                     False → transition to FAILED (deny)

        Returns a dict with:
            - exited: bool
            - operation_state: str
            - exit_receipt: dict | None
            - reason: str
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {"exited": False, "operation_state": None,
                    "exit_receipt": None,
                    "reason": f"Operation '{idempotency_key}' not found."}

        if record.state != OperationState.ESCALATED:
            return {"exited": False, "operation_state": record.state.value,
                    "exit_receipt": None,
                    "reason": f"Operation is in state {record.state.value}, not ESCALATED."}

        if record.hold_record is None:
            return {"exited": False, "operation_state": record.state.value,
                    "exit_receipt": None,
                    "reason": "Cannot authorize exit without an initialized HoldRecord."}
        if not all(isinstance(value, str) for value in (authorized_by, scope, authorization, reason)):
            return {
                "exited": False, "operation_state": record.state.value,
                "exit_receipt": None,
                "reason": "Authorized exit metadata must be strings.",
            }
        if not (authorized_by.strip() and scope.strip() and authorization.strip()):
            return {
                "exited": False, "operation_state": record.state.value,
                "exit_receipt": None,
                "reason": "Authorized exit requires non-empty operator, scope, and authorization metadata.",
            }

        receipt = ExitReceipt(
            authorized_by=authorized_by,
            scope=scope,
            timestamp=_utc_now(),
            authorization=authorization,
            reason=reason,
        )

        target_state = OperationState.RESERVED if approve else OperationState.FAILED
        auth_class = (
            "ESCALATED_AUTHORIZED_APPROVAL" if approve
            else "ESCALATED_AUTHORIZED_DENIAL"
        )
        evidence = _TransitionEvidence(
            idempotency_key, OperationState.ESCALATED, target_state,
            auth_class, "authorized_exit_receipt",
            (receipt.authorized_by, receipt.scope, receipt.authorization,
             receipt.timestamp, receipt.reason),
        )
        try:
            capability = self._registry._issue_transition_authorization(
                idempotency_key, OperationState.ESCALATED, target_state,
                auth_class, evidence, issuer=self,
            )
        except (RuntimeError, PermissionError, ValueError) as exc:
            current = self._registry.get(idempotency_key)
            return {"exited": False,
                    "operation_state": current.state.value if current else None,
                    "exit_receipt": None,
                    "reason": f"Transition authorization rejected: {exc}"}
        success, _prev = self._registry.transition_to(
            idempotency_key, target_state, authorization=capability,
            hold_record_updates={
                "exit_receipt": receipt,
                "exit_reason": "authorized_exit",
            },
        )

        return {
            "exited": success,
            "operation_state": self._registry.get(idempotency_key).state.value if self._registry.get(idempotency_key) else None,
            "exit_receipt": receipt.to_dict() if success else None,
            "reason": f"Authorized exit ({'approved' if approve else 'denied'}) by {authorized_by}." if success
                      else f"Transition ESCALATED → {target_state.value} failed.",
        }

    def expire_hold(self, idempotency_key: str) -> dict[str, Any]:
        """Transition HOLD → FAILED when τ_K is exhausted.

        Per doctrine: when τ_K expires on an unresolved HOLD, the default
        behavior is FAIL-CLOSED: the action is rejected. This is NOT a
        resolution — it is a fail-closed rejection.

        Per Blueprint v4.4: τ_K de composition expiré → Fail-Closed Collectif.

        Returns a dict with:
            - expired: bool
            - operation_state: str
            - reason: str
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {"expired": False, "operation_state": None,
                    "reason": f"Operation '{idempotency_key}' not found."}

        if record.state != OperationState.HOLD:
            return {"expired": False, "operation_state": record.state.value,
                    "reason": f"Operation is in state {record.state.value}, not HOLD."}

        expiry = self.check_tau_k_expiry(idempotency_key)
        if not expiry.get("expired"):
            return {
                "expired": False, "operation_state": record.state.value,
                "reason": "τ_K has not been verified as expired; HOLD remains unchanged.",
            }

        if record.hold_record is None:
            return {"expired": False, "operation_state": record.state.value,
                    "reason": "Cannot expire HOLD without an initialized HoldRecord."}
        auth_class = "HOLD_EXPIRY"
        evidence = _TransitionEvidence(
            idempotency_key, OperationState.HOLD, OperationState.FAILED,
            auth_class, "tau_k_expiry",
            (expiry.get("reason", ""), str(record.hold_record.tau_k_seconds)),
        )
        try:
            capability = self._registry._issue_transition_authorization(
                idempotency_key, OperationState.HOLD, OperationState.FAILED,
                auth_class, evidence, issuer=self,
            )
        except (RuntimeError, PermissionError, ValueError) as exc:
            current = self._registry.get(idempotency_key)
            return {"expired": False,
                    "operation_state": current.state.value if current else None,
                    "reason": f"Expiry authorization rejected: {exc}"}
        success, _prev = self._registry.transition_to(
            idempotency_key, OperationState.FAILED, authorization=capability,
            hold_record_updates={"exit_reason": "expired"},
        )
        return {
            "expired": success,
            "operation_state": self._registry.get(idempotency_key).state.value if self._registry.get(idempotency_key) else None,
            "reason": "τ_K exhausted — FAIL-CLOSED rejection (not a resolution)." if success
                      else "Transition HOLD → FAILED failed.",
        }

    def expire_escalated(self, idempotency_key: str) -> dict[str, Any]:
        """Transition ESCALATED → UNKNOWN when τ_K expires on an escalated hold.

        Per v7.1 §19: a time-based exit from ESCALATED is rejected. Expiry
        produces UNKNOWN, not resolution. This is the fail-closed path for
        escalated operations where no authorized exit was provided in time.

        Returns a dict with:
            - expired: bool
            - operation_state: str
            - reason: str
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {"expired": False, "operation_state": None,
                    "reason": f"Operation '{idempotency_key}' not found."}

        if record.state != OperationState.ESCALATED:
            return {"expired": False, "operation_state": record.state.value,
                    "reason": f"Operation is in state {record.state.value}, not ESCALATED."}

        expiry = self.check_tau_k_expiry(idempotency_key)
        if not expiry.get("expired"):
            return {
                "expired": False, "operation_state": record.state.value,
                "reason": "τ_K has not been verified as expired; ESCALATED remains unchanged.",
            }

        if record.hold_record is None:
            return {"expired": False, "operation_state": record.state.value,
                    "reason": "Cannot expire ESCALATED without an initialized HoldRecord."}
        auth_class = "ESCALATED_EXPIRY"
        evidence = _TransitionEvidence(
            idempotency_key, OperationState.ESCALATED, OperationState.UNKNOWN,
            auth_class, "tau_k_expiry",
            (expiry.get("reason", ""), str(record.hold_record.tau_k_seconds)),
        )
        try:
            capability = self._registry._issue_transition_authorization(
                idempotency_key, OperationState.ESCALATED, OperationState.UNKNOWN,
                auth_class, evidence, issuer=self,
            )
        except (RuntimeError, PermissionError, ValueError) as exc:
            current = self._registry.get(idempotency_key)
            return {"expired": False,
                    "operation_state": current.state.value if current else None,
                    "reason": f"Expiry authorization rejected: {exc}"}
        success, _prev = self._registry.transition_to(
            idempotency_key, OperationState.UNKNOWN, authorization=capability,
            hold_record_updates={"exit_reason": "expired_escalated"},
        )
        return {
            "expired": success,
            "operation_state": self._registry.get(idempotency_key).state.value if self._registry.get(idempotency_key) else None,
            "reason": "τ_K expired on ESCALATED — UNKNOWN (time-based exit rejected, needs human)." if success
                      else "Transition ESCALATED → UNKNOWN failed.",
        }

    def check_tau_k_expiry(self, idempotency_key: str, current_epoch: float | None = None) -> dict[str, Any]:
        """Check whether τ_K has expired for a HOLD or ESCALATED operation.

        Returns a dict with:
            - expired: bool (whether τ_K has expired)
            - operation_state: str
            - tau_k_remaining: float (seconds remaining, or 0)
            - reason: str
        """
        record = self._registry.get(idempotency_key)
        if record is None:
            return {"expired": False, "operation_state": None,
                    "tau_k_remaining": 0.0,
                    "reason": f"Operation '{idempotency_key}' not found."}

        if record.state not in (OperationState.HOLD, OperationState.ESCALATED):
            return {"expired": False, "operation_state": record.state.value,
                    "tau_k_remaining": 0.0,
                    "reason": f"Operation is in state {record.state.value}, not in HOLD lifecycle."}

        if not record.hold_record or record.hold_record.tau_k_seconds <= 0:
            return {"expired": False, "operation_state": record.state.value,
                    "tau_k_remaining": 0.0,
                    "reason": "No τ_K bound set — cannot check expiry."}

        now = current_epoch or _utc_now_seconds()
        hold_start_epoch = datetime.fromisoformat(
            record.hold_record.hold_start
        ).timestamp()
        elapsed = now - hold_start_epoch
        remaining = record.hold_record.tau_k_seconds - elapsed

        return {
            "expired": remaining <= 0,
            "operation_state": record.state.value,
            "tau_k_remaining": max(0.0, remaining),
            "reason": f"τ_K remaining: {max(0.0, remaining):.1f}s" if remaining > 0
                      else f"τ_K exhausted (elapsed {elapsed:.1f}s, bound {record.hold_record.tau_k_seconds}s)",
        }

    def get_hold_record(self, idempotency_key: str) -> HoldRecord | None:
        """Retrieve the HoldRecord for an operation, if any."""
        record = self._registry.get(idempotency_key)
        if record is None:
            return None
        return record.hold_record

    def find_holds(self) -> list[OperationRecord]:
        """Find all operations currently in HOLD state."""
        return self._registry.find_by_state(OperationState.HOLD)

    def find_escalated(self) -> list[OperationRecord]:
        """Find all operations currently in ESCALATED state."""
        return self._registry.find_by_state(OperationState.ESCALATED)

    def find_unresolved_holds(self) -> list[OperationRecord]:
        """Find all operations in HOLD or ESCALATED state.

        Per review 3: recovery must find unresolved operations and report them.
        HOLD operations need evidence or τ_K check; ESCALATED operations need
        authorized exit.
        """
        holds = self._registry.find_by_state(OperationState.HOLD)
        escalated = self._registry.find_by_state(OperationState.ESCALATED)
        return holds + escalated
