"""Opt-in native-claim adapter for the repository DispatchProvider protocol.

This module is intentionally not imported by DualKernelGovernor. A real
provider response is not itself Ed25519-signed by Stripe; therefore this
adapter requires a separately configured connector attestor whose key is
anchored by NativePositivePermitAuthority. Before confirming an effect it
also requires the provider's authenticated reconcile() API to report
CONFIRMED. Missing/ambiguous evidence remains UNKNOWN with I8 held. FAILED or
contradictory evidence is frozen for manual review; this tranche never releases
budget based on an unverified failure.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tenir_governance.operations.provider import ProviderOutcome
from w2_2b_canonical import request_hash as canon_request_hash
from w2_integration.native_dispatch_command import snapshot_command


_UNSET = object()


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


class Ed25519ConnectorAttestor:
    """Signs connector attestations after the adapter independently reconciles.

    The private key is injected by the caller and is never persisted here or in
    SQLite. It is a connector attestation key, not a claim that Stripe or another
    provider signed its own response.
    """

    def __init__(self, private_key: Ed25519PrivateKey):
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("Ed25519 connector attestor key required")
        self._private_key = private_key
        self.public_key = private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()

    def attest_confirmed(
        self, *, operation_key: str, idempotency_key: str, payload: dict,
        reconciliation: Any,
    ) -> dict:
        if (not getattr(reconciliation, "resolved", False)
                or getattr(reconciliation, "outcome", None) != ProviderOutcome.CONFIRMED):
            raise RuntimeError("only a resolved provider CONFIRMED result can be attested")
        provider_reference = getattr(reconciliation, "provider_reference", "")
        evidence = getattr(reconciliation, "raw_response", None)
        if not isinstance(provider_reference, str) or not provider_reference:
            raise RuntimeError("reconciled provider reference required")
        if not isinstance(evidence, dict):
            raise RuntimeError("reconciled provider evidence object required")
        body = {
            "attestation_type": "connector-reconciled-provider/v1",
            "operation_id": operation_key,
            "idempotency_key": idempotency_key,
            "request_hash": canon_request_hash(payload),
            "effect_status": "EXECUTED",
            "provider_reference": provider_reference,
            "evidence_digest": hashlib.sha256(_canonical(evidence)).hexdigest(),
            "key_id": self.key_id,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        signature = self._private_key.sign(_canonical(body))
        return {**body, "provider_signature": base64.b64encode(signature).decode("ascii")}


class NativeClaimedDispatchAdapter:
    """Separate, explicit adapter; never changes the historical Governor path."""

    def __init__(self, authority, provider, attestor: Ed25519ConnectorAttestor):
        if (not callable(getattr(authority, "consume_and_claim", None))
                or not callable(getattr(authority, "load_dispatch_payload", None))):
            raise TypeError("native claim authority required")
        if not callable(getattr(provider, "execute", None)) or not callable(
            getattr(provider, "reconcile", None)
        ):
            raise TypeError("DispatchProvider execute/reconcile contract required")
        if not isinstance(attestor, Ed25519ConnectorAttestor):
            raise TypeError("explicit Ed25519 connector attestor required")
        if getattr(authority, "trusted_provider_key_id", None) != attestor.key_id:
            raise RuntimeError("connector attestor key is not anchored by native authority")
        self.authority = authority
        self.provider = provider
        self.attestor = attestor

    def dispatch(
        self, operation_key: str, idempotency_key: str, payload: dict, *,
        action_type=_UNSET, amount=_UNSET, agent_id=_UNSET, metadata=_UNSET,
    ) -> dict:
        """Derive the provider arguments exclusively from the durable command.

        Replayed UNKNOWN claims are reconciled only; they never call execute()
        again. FAILED or conflicting outcomes receive a durable review marker
        and retain the I8 reservation for human resolution.

        Legacy keyword arguments are optional assertions, not argument sources.
        A sparse historical payload requires a newly approved complete command.
        The send right is granted once. Replay, crash and a review latched
        before that grant do not call the provider.
        """
        command = snapshot_command(payload)
        for name, value in (("action_type", action_type), ("amount", amount),
                            ("agent_id", agent_id), ("metadata", metadata)):
            if value is _UNSET:
                continue
            expected = command[name]
            if name == "amount":
                matches = type(value) in (int, float) and value == expected
            else:
                try:
                    matches = canon_request_hash({name: value}) == canon_request_hash({name: expected})
                except (TypeError, ValueError):
                    matches = False
            if not matches:
                raise RuntimeError(f"provider {name} does not match authorized command")
        if self.authority.requires_manual_review(operation_key):
            return {"status": "HOLD", "claim": None,
                    "reason": "manual review precedes send"}
        try:
            claim = self.authority.consume_and_claim(
                operation_key, command, idempotency_key
            )
        except RuntimeError as exc:
            if "manual review precedes send" not in str(exc):
                raise
            return {"status": "HOLD", "claim": None, "reason": str(exc)}
        persisted = snapshot_command(self.authority.load_dispatch_payload(claim))
        if canon_request_hash(persisted) != canon_request_hash(command):
            raise RuntimeError("persisted command differs from authorized command")
        command = persisted
        if self.authority.requires_manual_review(operation_key):
            return {"status": "HOLD", "claim": claim,
                    "reason": "durable provider conflict requires human review"}
        if claim.get("status") == "CONFIRMED":
            receipt = claim.get("receipt")
            if not self.authority._verify_provider_receipt(receipt):
                raise RuntimeError("stored connector attestation invalid")
            return {"status": "CONFIRMED", "claim": claim,
                    "receipt": receipt, "replayed": True}

        if claim.get("status") == "UNKNOWN":
            return self._reconcile_and_settle(
                claim, operation_key, idempotency_key, command,
                provider_reference=claim.get("provider_reference", ""),
            )

        decision = self.authority.grant_send_right(claim)
        if decision == "REVIEW":
            return {"status": "HOLD", "claim": claim,
                    "reason": "manual review precedes send"}
        if decision == "EXPIRED":
            return {"status": "HOLD", "claim": claim, "reason": "permit expired"}
        if decision != "GRANTED":
            try:
                self.authority.mark_dispatch_unknown(claim)
            except RuntimeError:
                pass
            return {"status": "UNKNOWN", "claim": claim, "replayed": True,
                    "reason": "send right already granted"}

        try:
            result = self.provider.execute(
                idempotency_key=idempotency_key,
                action_type=command["action_type"],
                amount=command["amount"],
                agent_id=command["agent_id"],
                metadata=deepcopy(command["metadata"]),
            )
        except Exception as exc:
            self.authority.mark_dispatch_unknown(claim)
            return {"status": "UNKNOWN", "claim": claim,
                    "reason": f"provider execute exception: {exc}"}

        outcome = getattr(result, "outcome", None)
        if outcome == ProviderOutcome.FAILED:
            self.authority.mark_dispatch_review(
                claim, "provider reported FAILED; no automated I8 release is configured"
            )
            return {"status": "HOLD", "claim": claim,
                    "reason": "provider failure retained for manual review"}
        if outcome != ProviderOutcome.CONFIRMED:
            self.authority.mark_dispatch_unknown(claim)
            return {"status": "UNKNOWN", "claim": claim,
                    "reason": "provider outcome is pending or ambiguous"}

        return self._reconcile_and_settle(
            claim, operation_key, idempotency_key, command,
            provider_reference=getattr(result, "provider_reference", ""),
            initial_outcome=outcome,
        )

    def _reconcile_and_settle(
        self, claim: dict, operation_key: str, idempotency_key: str,
        payload: dict, *, provider_reference: str = "", initial_outcome=None,
    ) -> dict:
        try:
            reconciliation = self.provider.reconcile(
                idempotency_key, provider_reference=provider_reference
            )
        except Exception as exc:
            self.authority.mark_dispatch_unknown(claim)
            return {"status": "UNKNOWN", "claim": claim,
                    "reason": f"provider reconciliation exception: {exc}"}

        if not getattr(reconciliation, "resolved", False):
            self.authority.mark_dispatch_unknown(claim)
            return {"status": "UNKNOWN", "claim": claim,
                    "reconciliation": reconciliation,
                    "reason": "provider could not establish a terminal result"}

        reconciled_outcome = getattr(reconciliation, "outcome", None)
        if reconciled_outcome != ProviderOutcome.CONFIRMED:
            reason = (
                "provider reconciliation FAILED; automatic I8 release is disabled"
                if reconciled_outcome == ProviderOutcome.FAILED
                else "provider reconciliation contradicted execution evidence"
            )
            self.authority.mark_dispatch_review(claim, reason)
            return {"status": "HOLD", "claim": claim,
                    "reconciliation": reconciliation, "reason": reason}

        # Initial FAILED and later CONFIRMED are contradictory. Never choose one
        # side automatically even if a provider later reports success.
        if initial_outcome == ProviderOutcome.FAILED:
            reason = "provider execute FAILED conflicts with reconciled CONFIRMED"
            self.authority.mark_dispatch_review(claim, reason)
            return {"status": "HOLD", "claim": claim,
                    "reconciliation": reconciliation, "reason": reason}

        try:
            receipt = self.attestor.attest_confirmed(
                operation_key=operation_key,
                idempotency_key=idempotency_key,
                payload=payload,
                reconciliation=reconciliation,
            )
            self.authority.mark_dispatch_confirmed(claim, receipt)
        except Exception as exc:
            self.authority.mark_dispatch_unknown(claim)
            return {"status": "UNKNOWN", "claim": claim,
                    "reconciliation": reconciliation,
                    "reason": f"connector attestation/settlement failed: {exc}"}
        return {"status": "CONFIRMED", "claim": claim,
                "receipt": receipt, "reconciliation": reconciliation,
                "replayed": bool(claim.get("replayed"))}
