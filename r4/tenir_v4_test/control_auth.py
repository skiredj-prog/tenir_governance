from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping


def _parse_timestamp(name: str, value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty ISO-8601 string")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone information")
    return parsed.astimezone(timezone.utc)


def _require_non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be provided")


@dataclass(frozen=True)
class TransitionProof:
    operator_id: str
    action: str
    reason: str
    nonce: str
    issued_at: str
    expires_at: str
    signature: str

    def validate(self) -> None:
        _require_non_empty("operator_id", self.operator_id)
        _require_non_empty("action", self.action)
        _require_non_empty("reason", self.reason)
        _require_non_empty("nonce", self.nonce)
        _require_non_empty("signature", self.signature)
        if len(self.signature.strip()) != 64:
            raise ValueError("signature must be a 64-character SHA-256 hex digest")
        if self.action not in {"SHADOW_TO_ENFORCE", "ENFORCE_TO_SHADOW"}:
            raise ValueError("action must be a supported control transition")
        issued_at = _parse_timestamp("issued_at", self.issued_at)
        expires_at = _parse_timestamp("expires_at", self.expires_at)
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")


class OperatorRegistry:
    """Shared-secret registry for explicit control-transition authorization."""

    def __init__(self, operators: Mapping[str, str] | None = None) -> None:
        self._operators: dict[str, str] = {}
        if operators:
            for operator_id, secret in operators.items():
                self.register_operator(operator_id, secret)

    def register_operator(self, operator_id: str, secret: str) -> None:
        _require_non_empty("operator_id", operator_id)
        _require_non_empty("secret", secret)
        self._operators[operator_id.strip()] = secret

    def _secret_for(self, operator_id: str) -> str:
        try:
            return self._operators[operator_id]
        except KeyError as exc:
            raise ValueError("unknown operator") from exc

    @staticmethod
    def _canonical_payload(
        *,
        operator_id: str,
        action: str,
        reason: str,
        nonce: str,
        issued_at: str,
        expires_at: str,
    ) -> str:
        payload = {
            "action": action,
            "expires_at": expires_at,
            "issued_at": issued_at,
            "nonce": nonce,
            "operator_id": operator_id,
            "reason": reason,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def sign_transition(
        self,
        *,
        operator_id: str,
        action: str,
        reason: str,
        nonce: str,
        issued_at: str,
        expires_at: str,
    ) -> str:
        message = self._canonical_payload(
            operator_id=operator_id,
            action=action,
            reason=reason,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        secret = self._secret_for(operator_id).encode("utf-8")
        return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue_transition_proof(
        self,
        *,
        operator_id: str,
        action: str,
        reason: str,
        ttl_seconds: int = 300,
        nonce: str | None = None,
        issued_at: str | None = None,
    ) -> TransitionProof:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        now = (
            _parse_timestamp("issued_at", issued_at)
            if issued_at is not None
            else datetime.now(timezone.utc)
        )
        expires = now + timedelta(seconds=ttl_seconds)
        proof = TransitionProof(
            operator_id=operator_id.strip(),
            action=action.strip(),
            reason=reason.strip(),
            nonce=(nonce or secrets.token_hex(16)).strip(),
            issued_at=now.isoformat(),
            expires_at=expires.isoformat(),
            signature="0" * 64,
        )
        signature = self.sign_transition(
            operator_id=proof.operator_id,
            action=proof.action,
            reason=proof.reason,
            nonce=proof.nonce,
            issued_at=proof.issued_at,
            expires_at=proof.expires_at,
        )
        return TransitionProof(
            operator_id=proof.operator_id,
            action=proof.action,
            reason=proof.reason,
            nonce=proof.nonce,
            issued_at=proof.issued_at,
            expires_at=proof.expires_at,
            signature=signature,
        )

    def verify_transition_proof(
        self,
        proof: TransitionProof,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        try:
            proof.validate()
            issued_at = _parse_timestamp("issued_at", proof.issued_at)
            expires_at = _parse_timestamp("expires_at", proof.expires_at)
        except (TypeError, ValueError) as exc:
            return False, str(exc)

        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if current_time < issued_at:
            return False, "proof is not yet valid"
        if current_time > expires_at:
            return False, "proof has expired"

        try:
            expected_signature = self.sign_transition(
                operator_id=proof.operator_id,
                action=proof.action,
                reason=proof.reason,
                nonce=proof.nonce,
                issued_at=proof.issued_at,
                expires_at=proof.expires_at,
            )
        except ValueError as exc:
            return False, str(exc)

        if not hmac.compare_digest(expected_signature, proof.signature):
            return False, "invalid signature"

        return True, "ok"
