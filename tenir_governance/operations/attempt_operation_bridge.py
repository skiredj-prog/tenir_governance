"""Fail-closed, observation-only bridge between real durable operations and T6/T7.

The operation registry and attempt ledger use DIFFERENT durable stores. There
is no atomic transaction across them. This adapter never alters OperationState,
dispatch, reservations or budget; it cannot authorize an external effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .attempt_qualification import AttemptQualificationStore, QualificationError
from .registry import DurableOperationRegistry, OperationRecord, OperationState
from .payload_binding import canonical_payload_hash


class OperationLinkError(QualificationError):
    """A source operation cannot safely be associated with candidate evidence."""


def operation_context_digest(record: OperationRecord) -> str:
    """Stable *identity* digest from an authoritative registry record.

    Mutable state/settlement fields are deliberately excluded. State is checked
    separately on each observation. The hash is not an execution grant.
    """
    if not isinstance(record, OperationRecord):
        raise OperationLinkError("authoritative OperationRecord required")
    for name in ("idempotency_key", "passage_id", "agent_id", "action_type"):
        value = getattr(record, name)
        if not isinstance(value, str) or not value.strip():
            raise OperationLinkError("incomplete immutable operation identity")
    context: dict[str, Any] = {
        "schema": "tenir.operation.qualification-binding.v1",
        "idempotency_key": record.idempotency_key,
        "passage_id": record.passage_id,
        "agent_id": record.agent_id,
        "action_type": record.action_type,
        "resource_id": record.resource_id,
        "amount": record.amount,
        "payload_hash": record.payload_hash,
        "operation_id": record.operation_id,
        "institutional_scope": record.institutional_scope,
        "payload_digest": record.payload_digest,
        "commit_gate_required": record.commit_gate_required,
    }
    try:
        return "sha256:" + canonical_payload_hash(context)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OperationLinkError("uncanonicalizable operation identity") from exc


@dataclass(frozen=True)
class OperationLinkObservation:
    status: str
    reason: str
    operation_key: str
    attempt_id: str
    operation_state: str | None = None
    attempt_state: str | None = None
    evidence_recorded: bool = False
    execution_authorized: bool = False  # Invariant: never set to True.


class AttemptOperationObservationBridge:
    """Create/inspect candidate evidence linked to persisted governor operations.

    Only an UNKNOWN durable operation may be enrolled. Recheck immutable
    identity and state on *every* read and after proof recording. Another
    worker may transition the operation at any time: observational matching
    cannot serve as a commit gate (TOCTOU and two-store non-atomicity).
    """

    def __init__(self, registry: DurableOperationRegistry,
                 attempts: AttemptQualificationStore):
        if not isinstance(registry, DurableOperationRegistry) or not registry.is_persistent:
            raise OperationLinkError("persistent operation registry required")
        if not isinstance(attempts, AttemptQualificationStore):
            raise OperationLinkError("durable candidate attempt ledger required")
        from .attempt_attestation import Ed25519AttemptVerifier
        if not isinstance(attempts.verifier, Ed25519AttemptVerifier):
            raise OperationLinkError("independently configured Ed25519 verifier required")
        self.registry = registry
        self.attempts = attempts

    def _source(self, key: str) -> OperationRecord:
        if not isinstance(key, str) or not key:
            raise OperationLinkError("operation key required")
        if self.registry.is_corrupted:
            raise OperationLinkError("corrupt operation registry")
        record = self.registry.get(key)
        if self.registry.is_corrupted:
            raise OperationLinkError("corrupt operation registry")
        if record is None or record.idempotency_key != key:
            raise OperationLinkError("source operation missing or mismatched")
        if record.state is not OperationState.UNKNOWN:
            raise OperationLinkError("source operation must be UNKNOWN")
        if self.registry.quarantine_is_active():
            raise OperationLinkError("operation registry quarantine active")
        return record

    def begin_observation(self, *, operation_key: str, attempt_id: str,
                          lei: str, nonce: str, deadline_ms: int
                          ) -> OperationLinkObservation:
        record = self._source(operation_key)
        digest = operation_context_digest(record)
        self.attempts.begin(
            attempt_id=attempt_id, lei=lei, nonce=nonce,
            operation_digest=digest, operation_key=operation_key,
            deadline_ms=deadline_ms,
        )
        # A second registry read cannot establish cross-store atomicity,
        # but can detect a changed operation and issue HOLD.
        return self.observe(operation_key=operation_key, attempt_id=attempt_id)

    def observe(self, *, operation_key: str,
                attempt_id: str) -> OperationLinkObservation:
        try:
            record = self._source(operation_key)
            attempt = self.attempts.read(attempt_id)
            if attempt.get("operation_key") != operation_key:
                raise OperationLinkError("attempt bound to different operation")
            if attempt.get("operation_digest") != operation_context_digest(record):
                raise OperationLinkError("source operation identity drift")
            if not attempt.get("lei") or not attempt.get("nonce"):
                raise OperationLinkError("missing attempt identity binding")
            return OperationLinkObservation(
                status="MATCHED_OBSERVATION_ONLY", reason="candidate binding matched",
                operation_key=operation_key, attempt_id=attempt_id,
                operation_state=record.state.value, attempt_state=attempt["state"],
                evidence_recorded=attempt["state"] in ("RESOLVED", "FAILED"),
            )
        except (OperationLinkError, QualificationError, KeyError, ValueError,
                RuntimeError, OSError) as exc:
            return OperationLinkObservation(
                status="HOLD", reason=f"candidate binding cannot be relied upon: {exc}",
                operation_key=operation_key, attempt_id=attempt_id,
            )

    def qualify_observation(self, *, operation_key: str, attempt_id: str,
                            signed_evidence: dict, observed_ms: int
                            ) -> OperationLinkObservation:
        before = self.observe(operation_key=operation_key, attempt_id=attempt_id)
        if before.status != "MATCHED_OBSERVATION_ONLY":
            return before
        try:
            self.attempts.qualify(attempt_id, signed_evidence, observed_ms=observed_ms)
        except (QualificationError, ValueError, RuntimeError, OSError) as exc:
            return OperationLinkObservation(
                status="HOLD", reason=f"evidence not accepted: {exc}",
                operation_key=operation_key, attempt_id=attempt_id,
            )
        after = self.observe(operation_key=operation_key, attempt_id=attempt_id)
        if after.status != "MATCHED_OBSERVATION_ONLY":
            return OperationLinkObservation(
                status="HOLD", reason="operation diverged during independent evidence write; "
                                      "manual reconciliation required",
                operation_key=operation_key, attempt_id=attempt_id,
                evidence_recorded=True,
            )
        return OperationLinkObservation(
            status="EVIDENCE_RECORDED_NO_COMMIT_AUTHORITY",
            reason="signed candidate evidence recorded; governor state unchanged",
            operation_key=operation_key, attempt_id=attempt_id,
            operation_state=after.operation_state,
            attempt_state=after.attempt_state,
            evidence_recorded=True,
        )


def inspect_governor_attempt(governor: object, attempts: AttemptQualificationStore,
                             *, operation_key: str, attempt_id: str) -> dict[str, Any]:
    """Read-only governor-facing report. Always HOLD execution.

    The governor's provider, adjudication, dispatch and budget paths are never
    called. A MATCHED observation is *not* a commit grant: the source state may
    change after the read and there is no transaction across the two stores.
    """
    try:
        from ..composition.governor import DualKernelGovernor
        if not isinstance(governor, DualKernelGovernor):
            raise OperationLinkError("actual DualKernelGovernor instance required")
        bridge = AttemptOperationObservationBridge(governor.registry, attempts)
        observation = bridge.observe(
            operation_key=operation_key, attempt_id=attempt_id,
        )
        return {
            "decision": "HOLD",
            "execution_authorized": False,
            "candidate_status": observation.status,
            "candidate_reason": observation.reason,
            "operation_state": observation.operation_state,
            "attempt_state": observation.attempt_state,
            "evidence_recorded": observation.evidence_recorded,
            "operation_key": operation_key,
            "attempt_id": attempt_id,
            "source": "isolated_t6_t7_observation",
        }
    except (OperationLinkError, QualificationError, RuntimeError,
            KeyError, ValueError, OSError) as exc:
        return {
            "decision": "HOLD",
            "execution_authorized": False,
            "candidate_status": "HOLD",
            "candidate_reason": f"unavailable: {exc}",
            "operation_key": operation_key,
            "attempt_id": attempt_id,
            "source": "isolated_t6_t7_observation",
        }
