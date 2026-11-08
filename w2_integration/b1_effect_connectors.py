"""HTTP/Stripe contract adapters restricted to an in-process mock transport.

No live credentials or real network transport can be configured here. Evidence
is synthetic and does not establish a provider's independent attestation.
"""
from decimal import Decimal
import re

import httpx


def unknown():
    return {"status": "UNKNOWN"}


class MockEffectConnector:
    def __init__(self, transport, *, destinations):
        if type(transport) is not httpx.MockTransport:
            raise RuntimeError("only local httpx.MockTransport is allowed")
        self._client = httpx.Client(transport=transport, base_url="https://provider.invalid",
                                    timeout=1.0, follow_redirects=False, trust_env=False)
        self.destinations = frozenset(destinations)

    def close(self):
        self._client.close()

    def validate(self, wire):
        if wire["payload"]["destination"] not in self.destinations:
            raise ValueError("destination is not configured")

    def execute(self, wire):
        self.validate(wire)
        try:
            response = self._client.post("/api/v1/effects/execute", json=wire,
                                         headers={"Idempotency-Key": wire["idempotency_key"]})
            return self._observation(response, wire)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return unknown()

    def reconcile(self, wire, reference=""):
        try:
            response = self._client.get("/api/v1/effects/status/" + wire["operation_id"])
            return self._observation(response, wire)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return unknown()

    @staticmethod
    def _observation(response, wire):
        data = response.json()
        if not isinstance(data, dict) or data.get("operation_id") != wire["operation_id"]:
            return unknown()
        if response.status_code == 202 and data.get("status", "PENDING") == "PENDING":
            return {"status": "PENDING"}
        if data.get("idempotency_key") != wire["idempotency_key"]:
            return unknown()
        if response.status_code == 200 and data.get("status") == "PENDING":
            return {"status": "PENDING"}
        if response.status_code == 200 and data.get("status") == "CONFIRMED":
            receipt = data.get("receipt")
            if (isinstance(receipt, dict) and receipt.get("intent_hash") == wire["idempotency_key"]
                    and isinstance(receipt.get("provider_reference"), str) and receipt["provider_reference"]):
                return {"status": "CONFIRMED", "reference": receipt["provider_reference"], "evidence": data}
        error = data.get("error")
        # A bare HTTP 400 is not proof of no effect. The local mock contract
        # includes an explicit, correlated no-effect observation.
        if (response.status_code in (200, 400) and data.get("status") == "FAILED"
                and isinstance(error, dict) and error.get("no_effect") is True
                and isinstance(error.get("code"), str) and error["code"]):
            return {"status": "FAILED", "error": {"code": "FAILED_PROVEN", "provider_code": error["code"]},
                    "evidence": data}
        return unknown()


class MockStripeConnector(MockEffectConnector):
    """Create+confirm a PaymentIntent, or confirm a pre-bound existing ID."""

    def validate(self, wire):
        super().validate(wire)
        if not re.fullmatch(r"(?:pm|pi)_[A-Za-z0-9_]+", wire["payload"]["destination"]):
            raise ValueError("Stripe destination must be a configured pm_ or pi_ identifier")

    def execute(self, wire):
        self.validate(wire)
        payload = wire["payload"]
        try:
            destination = payload["destination"]
            if destination.startswith("pi_"):
                initial = self._client.get("/v1/payment_intents/" + destination)
                if initial.status_code != 200 or not self._bound(initial.json(), wire):
                    return unknown()
                data = initial.json()
                if data["status"] != "requires_confirmation":
                    return self._observation(initial, wire)
                response = self._client.post("/v1/payment_intents/" + destination + "/confirm",
                                             data={}, headers={"Idempotency-Key": wire["operation_id"]})
            else:
                response = self._client.post("/v1/payment_intents", data={
                    "amount": int(Decimal(str(payload["amount"])) * 100),
                    "currency": payload["currency"].lower(), "payment_method": destination, "confirm": "true",
                    "metadata[tenir_operation_id]": wire["operation_id"],
                    "metadata[tenir_intent_hash]": wire["idempotency_key"],
                }, headers={"Idempotency-Key": wire["operation_id"]})
            return self._observation(response, wire)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return unknown()

    def reconcile(self, wire, reference=""):
        try:
            destination = wire["payload"]["destination"]
            if not reference and destination.startswith("pi_"):
                reference = destination
            if not reference:
                # Stripe exposes search by metadata, not a lookup endpoint
                # keyed by Idempotency-Key. Empty/ambiguous search is UNKNOWN.
                response = self._client.get("/v1/payment_intents/search", params={
                    "query": "metadata['tenir_operation_id']:'" + wire["operation_id"] + "'"})
                data = response.json()
                if (response.status_code != 200 or not isinstance(data, dict)
                        or data.get("has_more") is not False or not isinstance(data.get("data"), list)
                        or len(data["data"]) != 1 or not self._bound(data["data"][0], wire)):
                    return unknown()
                reference = data["data"][0]["id"]
            if not re.fullmatch(r"pi_[A-Za-z0-9_]+", reference):
                return unknown()
            response = self._client.get("/v1/payment_intents/" + reference)
            if response.status_code == 200 and response.json().get("id") != reference:
                return unknown()
            return self._observation(response, wire)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            return unknown()

    @staticmethod
    def _bound(data, wire):
        if not isinstance(data, dict):
            return False
        metadata, payload = data.get("metadata"), wire["payload"]
        return (data.get("object") == "payment_intent" and data.get("livemode") is False
                and isinstance(data.get("id"), str) and re.fullmatch(r"pi_[A-Za-z0-9_]+", data["id"]) is not None
                and type(data.get("amount")) is int and data["amount"] == int(Decimal(str(payload["amount"])) * 100)
                and data.get("currency") == payload["currency"].lower()
                and (not payload["destination"].startswith("pm_") or data.get("payment_method") == payload["destination"])
                and (not payload["destination"].startswith("pi_") or data["id"] == payload["destination"])
                and isinstance(metadata, dict) and metadata.get("tenir_operation_id") == wire["operation_id"]
                and metadata.get("tenir_intent_hash") == wire["idempotency_key"])

    @classmethod
    def _observation(cls, response, wire):
        data = response.json()
        if response.status_code == 200 and cls._bound(data, wire):
            if data.get("status") == "succeeded":
                if data.get("amount_received") != data["amount"]:
                    return unknown()
                return {"status": "CONFIRMED", "reference": data["id"], "evidence": data}
            if data.get("status") in ("processing", "requires_action", "requires_confirmation", "requires_capture"):
                return {"status": "PENDING", "reference": data["id"]}
            return {"status": "UNKNOWN", "reference": data["id"]}
        # Explicit local fixture observation; generic Stripe 400/402 responses
        # never establish FAILED_PROVEN automatically.
        error = data.get("error") if isinstance(data, dict) else None
        if (response.status_code == 400 and isinstance(error, dict) and error.get("no_effect") is True
                and error.get("operation_id") == wire["operation_id"]
                and error.get("intent_hash") == wire["idempotency_key"] and error.get("code") == "mock_pre_execution_rejection"):
            return {"status": "FAILED", "error": {"code": "FAILED_PROVEN", "provider_code": error["code"]},
                    "evidence": data}
        return unknown()
