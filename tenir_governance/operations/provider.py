"""Dispatch provider abstraction for real external execution.

This module defines the protocol that external execution providers must
implement, along with a MockProvider for testing and a StripeProvider
adapter for production use.

The key design principle: the governance layer controls the dispatch
state machine (CREATED → RESERVED → DISPATCHING → CONFIRMED|FAILED|UNKNOWN),
and the provider is called ONLY after the governor has atomically
transitioned to DISPATCHING. This means:

1. The idempotency key is durably claimed before the provider is called.
2. If the provider call times out, the state remains DISPATCHING — the
   caller must reconcile to determine the actual outcome.
3. The provider receives the idempotency key so it can deduplicate
   on its side (provider-side idempotency is required for exactly-once
   effects per Helland 2007).

Per the guarantee boundary:
- Locally guaranteed: the state transition to DISPATCHING is atomic.
- Requires distributed infrastructure: cross-host coordination.
- Depends on external provider behavior: provider-side idempotency key
  support for exactly-once external effects.

Profile: agentic-governance-profile-v1.0.0
Compliance class: profile-bound
This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ProviderOutcome(str, Enum):
    """Outcome of a provider execution attempt.

    These map directly to the dispatch state machine:
        CONFIRMED — the external effect occurred successfully
        FAILED    — the external system confirmed no effect occurred
        PENDING   — the request was accepted but result not yet known
        UNKNOWN   — the call timed out or the response was ambiguous

    Per doctrine v7.1: a timeout does NOT prove no effect occurred.
    UNKNOWN triggers reconciliation, not auto-failure.
    """
    CONFIRMED = "confirmed"
    FAILED = "failed"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass
class DispatchResult:
    """Result of a provider execution attempt.

    Attributes:
        outcome: What happened (confirmed/failed/pending/unknown).
        provider_reference: Provider-side reference for the transaction
            (e.g., Stripe charge ID, PayPal capture ID).
        raw_response: The raw provider response for audit/logging.
        error: Error message if the call failed or timed out.
        latency_ms: Time taken for the provider call in milliseconds.
    """
    outcome: ProviderOutcome
    provider_reference: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    latency_ms: float = 0.0

    @property
    def is_terminal(self) -> bool:
        """Is this a terminal outcome (confirmed or failed)?"""
        return self.outcome in (ProviderOutcome.CONFIRMED, ProviderOutcome.FAILED)


@dataclass
class ReconciliationResult:
    """Result of reconciling an UNKNOWN operation with the provider.

    Attributes:
        resolved: True if the provider could determine the outcome.
        outcome: The resolved outcome (confirmed/failed), or UNKNOWN
            if the provider could not determine it.
        provider_reference: Provider-side reference if available.
        raw_response: Raw provider response.
        error: Error message if reconciliation failed.
    """
    resolved: bool
    outcome: ProviderOutcome = ProviderOutcome.UNKNOWN
    provider_reference: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@runtime_checkable
class DispatchProvider(Protocol):
    """Protocol for external execution providers.

    A provider implements two methods:
        execute() — attempt the external effect
        reconcile() — check the status of a previously-unknown operation

    The provider MUST:
    1. Accept the idempotency key and pass it to the external system.
    2. Return CONFIRMED only when the external effect definitely occurred.
    3. Return FAILED only when the external system confirms no effect.
    4. Return UNKNOWN on timeout or ambiguous response — never FAILED.
    5. Be safe to call multiple times with the same idempotency key
       (provider-side deduplication is the provider's responsibility).
    """

    def execute(
        self,
        idempotency_key: str,
        action_type: str,
        amount: float,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        """Attempt to execute the external effect.

        Args:
            idempotency_key: The governance-assigned idempotency key.
                The provider MUST pass this to the external system so
                that duplicate calls are deduplicated on the provider side.
            action_type: The type of action (e.g., "payment", "transfer").
            amount: The monetary amount (if applicable).
            agent_id: The agent requesting the action.
            metadata: Additional provider-specific metadata.

        Returns:
            DispatchResult with the outcome.
        """
        ...

    def reconcile(
        self,
        idempotency_key: str,
        provider_reference: str = "",
    ) -> ReconciliationResult:
        """Reconcile a previously-UNKNOWN operation.

        Called when an operation is in UNKNOWN state (timeout or ambiguous
        response). The provider should check with the external system
        whether the effect occurred.

        Args:
            idempotency_key: The original idempotency key.
            provider_reference: Provider-side reference if available.

        Returns:
            ReconciliationResult indicating whether the outcome could
            be determined.
        """
        ...


class MockProvider:
    """Mock provider for testing with configurable behaviors.

    This provider simulates an external execution system. It can be
    configured to:
    - Always succeed (default)
    - Always fail
    - Always time out (return UNKNOWN)
    - Succeed after N calls (simulate retry)
    - Execute a custom callable

    The mock also records all calls for test assertions.

    Usage:
        provider = MockProvider()
        provider.behavior = "confirmed"
        result = provider.execute("key-1", "payment", 10.0, "alice")
        assert result.outcome == ProviderOutcome.CONFIRMED

        provider.behavior = "timeout"
        result = provider.execute("key-2", "payment", 20.0, "alice")
        assert result.outcome == ProviderOutcome.UNKNOWN

        # Reconcile the UNKNOWN
        recon = provider.reconcile("key-2")
        assert recon.resolved
        assert recon.outcome == ProviderOutcome.CONFIRMED
    """

    def __init__(
        self,
        behavior: str = "confirmed",
        latency_ms: float = 1.0,
        reconcile_outcome: ProviderOutcome = ProviderOutcome.CONFIRMED,
    ):
        """
        Args:
            behavior: One of "confirmed", "failed", "timeout", "pending".
            latency_ms: Simulated latency in milliseconds.
            reconcile_outcome: What reconcile() returns for UNKNOWN ops.
        """
        self.behavior = behavior
        self.latency_ms = latency_ms
        self.reconcile_outcome = reconcile_outcome
        self.calls: list[dict[str, Any]] = []
        self.reconcile_calls: list[dict[str, Any]] = []
        self._execute_after_n: int | None = None
        self._call_count: int = 0
        self._custom_handler = None

    def set_execute_after_n(self, n: int):
        """Configure the mock to succeed only after N execute() calls.

        Calls 1..N-1 return UNKNOWN (timeout), call N returns CONFIRMED.
        Useful for testing retry/reconciliation logic.
        """
        self._execute_after_n = n
        self._call_count = 0

    def set_custom_handler(self, handler):
        """Set a custom callable for execute().

        The handler receives (idempotency_key, action_type, amount,
        agent_id, metadata) and must return a DispatchResult.
        """
        self._custom_handler = handler

    def execute(
        self,
        idempotency_key: str,
        action_type: str,
        amount: float,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        start = time.monotonic()

        # Record the call
        call_record = {
            "idempotency_key": idempotency_key,
            "action_type": action_type,
            "amount": amount,
            "agent_id": agent_id,
            "metadata": metadata or {},
            "timestamp": time.time(),
        }
        self.calls.append(call_record)

        # Simulate latency
        elapsed_ms = (time.monotonic() - start) * 1000

        # Custom handler takes priority
        if self._custom_handler is not None:
            return self._custom_handler(
                idempotency_key, action_type, amount, agent_id, metadata,
            )

        # Execute-after-N mode
        if self._execute_after_n is not None:
            self._call_count += 1
            if self._call_count < self._execute_after_n:
                return DispatchResult(
                    outcome=ProviderOutcome.UNKNOWN,
                    error="Simulated timeout",
                    latency_ms=elapsed_ms,
                )
            return DispatchResult(
                outcome=ProviderOutcome.CONFIRMED,
                provider_reference=f"mock_ref_{idempotency_key}",
                raw_response={"status": "succeeded", "idempotency_key": idempotency_key},
                latency_ms=elapsed_ms,
            )

        # Standard behaviors
        if self.behavior == "confirmed":
            return DispatchResult(
                outcome=ProviderOutcome.CONFIRMED,
                provider_reference=f"mock_ref_{idempotency_key}",
                raw_response={"status": "succeeded", "idempotency_key": idempotency_key},
                latency_ms=elapsed_ms,
            )
        elif self.behavior == "failed":
            return DispatchResult(
                outcome=ProviderOutcome.FAILED,
                error="Simulated provider failure",
                raw_response={"status": "failed", "idempotency_key": idempotency_key},
                latency_ms=elapsed_ms,
            )
        elif self.behavior == "timeout":
            return DispatchResult(
                outcome=ProviderOutcome.UNKNOWN,
                error="Simulated timeout",
                raw_response={"status": "timeout", "idempotency_key": idempotency_key},
                latency_ms=elapsed_ms,
            )
        elif self.behavior == "pending":
            return DispatchResult(
                outcome=ProviderOutcome.PENDING,
                provider_reference=f"mock_ref_{idempotency_key}",
                raw_response={"status": "pending", "idempotency_key": idempotency_key},
                latency_ms=elapsed_ms,
            )
        else:
            raise ValueError(f"Unknown behavior: {self.behavior}")

    def reconcile(
        self,
        idempotency_key: str,
        provider_reference: str = "",
    ) -> ReconciliationResult:
        self.reconcile_calls.append({
            "idempotency_key": idempotency_key,
            "provider_reference": provider_reference,
            "timestamp": time.time(),
        })

        # Simulated reconciliation always resolves
        return ReconciliationResult(
            resolved=True,
            outcome=self.reconcile_outcome,
            provider_reference=provider_reference or f"mock_ref_{idempotency_key}",
            raw_response={"status": self.reconcile_outcome.value, "idempotency_key": idempotency_key},
        )


class StripeProvider:
    """Stripe payment provider adapter with idempotency key support.

    This adapter wraps the Stripe SDK to execute payments with provider-side
    idempotency key support, which is required for exactly-once external effects.

    Per Helland 2007: exactly-once effects across service boundaries require
    provider-side deduplication. Stripe supports this via the
    Idempotency-Key header on API requests.

    Usage:
        provider = StripeProvider(api_key="sk_test_...")
        result = provider.execute(
            idempotency_key="op-123",
            action_type="payment",
            amount=10.00,
            agent_id="alice",
            metadata={"customer": "cus_123"},
        )

    Note: This adapter requires the `stripe` package. Install with:
        pip install tenir-governance[stripe]

    The adapter fails closed: any unhandled exception returns UNKNOWN,
    never CONFIRMED.
    """

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 30.0,
        currency: str = "usd",
    ):
        """
        Args:
            api_key: Stripe API key (sk_test_... or sk_live_...).
            timeout_seconds: Request timeout. On timeout, returns UNKNOWN.
            currency: Default currency for payments.
        """
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._currency = currency
        self._stripe = None  # Lazily imported

    def _get_stripe(self):
        """Lazily import and configure the Stripe SDK."""
        if self._stripe is None:
            try:
                import stripe
            except ImportError as e:
                raise ImportError(
                    "Stripe provider requires the 'stripe' package. "
                    "Install with: pip install tenir-governance[stripe]"
                ) from e
            stripe.api_key = self._api_key
            stripe.timeout = self._timeout
            self._stripe = stripe
        return self._stripe

    def execute(
        self,
        idempotency_key: str,
        action_type: str,
        amount: float,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        """Execute a payment via Stripe with idempotency key.

        The idempotency key is passed as the Stripe Idempotency-Key header,
        so duplicate calls with the same key are deduplicated by Stripe.
        """
        start = time.monotonic()
        metadata = metadata or {}

        try:
            stripe = self._get_stripe()

            # Convert amount to cents (Stripe requires integer cents)
            amount_cents = int(round(amount * 100))

            # Build Stripe metadata
            stripe_metadata = {
                "tenir_agent_id": agent_id,
                "tenir_action_type": action_type,
                "tenir_idempotency_key": idempotency_key,
                **{k: str(v) for k, v in metadata.items()},
            }

            # Create a PaymentIntent with the idempotency key
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency=self._currency,
                metadata=stripe_metadata,
                idempotency_key=idempotency_key,
                confirm=True,
                automatic_payment_methods={"enabled": True},
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            if intent.status == "succeeded":
                return DispatchResult(
                    outcome=ProviderOutcome.CONFIRMED,
                    provider_reference=intent.id,
                    raw_response={
                        "status": "succeeded",
                        "idempotency_key": idempotency_key,
                        "intent_id": intent.id,
                        "amount": amount,
                    },
                    latency_ms=elapsed_ms,
                )
            elif intent.status in ("requires_payment_method", "requires_action"):
                # Payment requires additional action — not confirmed yet
                return DispatchResult(
                    outcome=ProviderOutcome.PENDING,
                    provider_reference=intent.id,
                    raw_response={
                        "status": intent.status,
                        "idempotency_key": idempotency_key,
                        "intent_id": intent.id,
                    },
                    latency_ms=elapsed_ms,
                )
            else:
                # Ambiguous status — fail closed to UNKNOWN
                return DispatchResult(
                    outcome=ProviderOutcome.UNKNOWN,
                    provider_reference=intent.id if hasattr(intent, "id") else "",
                    raw_response={
                        "status": intent.status,
                        "idempotency_key": idempotency_key,
                    },
                    error=f"Unexpected Stripe status: {intent.status}",
                    latency_ms=elapsed_ms,
                )

        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_str = str(e)

            # Check if it's a timeout or connection error
            is_timeout = "timeout" in error_str.lower() or "connection" in error_str.lower()

            if is_timeout:
                # Per doctrine: timeout does NOT prove no effect occurred
                return DispatchResult(
                    outcome=ProviderOutcome.UNKNOWN,
                    error=f"Stripe timeout: {error_str}",
                    latency_ms=elapsed_ms,
                )

            # Check if it's a Stripe API error with a code
            if hasattr(e, "code") and e.code:
                # Card declined or similar — the payment definitively failed
                if e.code in ("card_declined", "expired_card", "incorrect_cvc",
                              "incorrect_number", "processing_error"):
                    return DispatchResult(
                        outcome=ProviderOutcome.FAILED,
                        error=f"Stripe error: {error_str}",
                        raw_response={"error_code": e.code, "idempotency_key": idempotency_key},
                        latency_ms=elapsed_ms,
                    )

            # Any other error — fail closed to UNKNOWN
            return DispatchResult(
                outcome=ProviderOutcome.UNKNOWN,
                error=f"Stripe error: {error_str}",
                latency_ms=elapsed_ms,
            )

    def reconcile(
        self,
        idempotency_key: str,
        provider_reference: str = "",
    ) -> ReconciliationResult:
        """Reconcile a previously-UNKNOWN operation with Stripe.

        If we have a provider reference (PaymentIntent ID), retrieve it.
        Otherwise, search for the PaymentIntent by idempotency key
        (via the metadata we stored).
        """
        try:
            stripe = self._get_stripe()

            if provider_reference:
                # Retrieve the specific PaymentIntent
                intent = stripe.PaymentIntent.retrieve(provider_reference)
            else:
                # Search by metadata
                intents = stripe.PaymentIntent.search(
                    query=f'metadata["tenir_idempotency_key"]:"{idempotency_key}"',
                    limit=1,
                )
                if not intents.data:
                    # No intent found — the payment likely never reached Stripe
                    return ReconciliationResult(
                        resolved=True,
                        outcome=ProviderOutcome.FAILED,
                        error="No PaymentIntent found for idempotency key",
                    )
                intent = intents.data[0]

            if intent.status == "succeeded":
                return ReconciliationResult(
                    resolved=True,
                    outcome=ProviderOutcome.CONFIRMED,
                    provider_reference=intent.id,
                    raw_response={"status": "succeeded", "intent_id": intent.id},
                )
            elif intent.status in ("canceled", "requires_payment_method"):
                return ReconciliationResult(
                    resolved=True,
                    outcome=ProviderOutcome.FAILED,
                    provider_reference=intent.id,
                    raw_response={"status": intent.status, "intent_id": intent.id},
                )
            else:
                # Still pending or in an ambiguous state
                return ReconciliationResult(
                    resolved=False,
                    outcome=ProviderOutcome.UNKNOWN,
                    provider_reference=intent.id,
                    raw_response={"status": intent.status, "intent_id": intent.id},
                    error=f"Stripe status still ambiguous: {intent.status}",
                )

        except Exception as e:
            return ReconciliationResult(
                resolved=False,
                outcome=ProviderOutcome.UNKNOWN,
                error=f"Reconciliation error: {e}",
            )