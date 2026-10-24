"""Authenticated external monotone CAS protocol for the EP-02 experiment.

The service delegates atomic state changes to one ``DurableAnchorCAS``
authority and signs every receipt. It is suitable as a protocol boundary for
remote clients, but it is not a replicated server, an HTTP implementation or a
multi-server consensus protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import secrets
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_durable_cas import (
    DurableAnchorCAS,
    DurableCASResponse,
    DurableCASResult,
    LookupStatus,
)
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
)


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MESSAGE = 1_048_576
_REQUEST_DOMAIN = "TENIR-EP02-EXTERNAL-CAS-REQUEST-V1"
_LOOKUP_DOMAIN = "TENIR-EP02-EXTERNAL-CAS-LOOKUP-V1"
_RECEIPT_DOMAIN = "TENIR-EP02-EXTERNAL-CAS-RECEIPT-V1"


class ExternalProtocolError(RuntimeError):
    """An external request, lookup or receipt is malformed or unauthenticated."""


class ExternalOutcomeUnknown(RuntimeError):
    """Transport failure left the caller unable to determine commit outcome."""


@dataclass(frozen=True)
class SignedExternalRequest:
    authority_id: str
    client_id: str
    client_generation: int
    request_id: str
    operation: str
    expected: TrustCheckpoint | None
    successor: SignedTrustState
    payload: bytes
    request_digest: str
    client_signature: bytes


@dataclass(frozen=True)
class SignedLookupQuery:
    request: SignedExternalRequest
    challenge: bytes
    payload: bytes
    client_signature: bytes


@dataclass(frozen=True)
class SignedExternalReceipt:
    anchor_id: str
    authority_id: str
    client_id: str
    client_generation: int
    request_id: str
    request_digest: str
    lookup_status: LookupStatus
    operation: str
    result: DurableCASResult | None
    reason: str | None
    observed_checkpoint: TrustCheckpoint | None
    result_checkpoint: TrustCheckpoint | None
    current_checkpoint: TrustCheckpoint | None
    sequence: int | None
    replayed: bool
    challenge: bytes | None
    signature: bytes


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _name(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _digest(value, label):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _checkpoint_body(value):
    if value is None:
        return None
    if (
        not isinstance(value, TrustCheckpoint)
        or type(value.revision) is not int
        or value.revision < 1
    ):
        raise ValueError("invalid checkpoint")
    _digest(value.snapshot_fingerprint, "checkpoint fingerprint")
    return {
        "revision": value.revision,
        "fingerprint": value.snapshot_fingerprint,
    }


def _checkpoint_from_body(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"revision", "fingerprint"}:
        raise ValueError("invalid checkpoint object")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValueError("invalid checkpoint revision")
    _digest(value["fingerprint"], "checkpoint fingerprint")
    return TrustCheckpoint(value["revision"], value["fingerprint"])


def _successor_body(successor):
    if not isinstance(successor, SignedTrustState):
        raise TypeError("expected SignedTrustState")
    payload = bound_snapshot_bytes(successor)
    if not isinstance(successor.signature, bytes) or len(successor.signature) != 64:
        raise ValueError("invalid successor signature")
    _name(successor.signer_id, "successor signer")
    return {
        "signer_id": successor.signer_id,
        "signature": successor.signature.hex(),
        "bound_payload": payload.hex(),
    }


def _request_body(
    *, authority_id, client_id, client_generation, request_id,
    operation, expected, successor,
):
    _name(authority_id, "authority identity")
    _name(client_id, "client identity")
    _name(request_id, "request identity")
    if type(client_generation) is not int or client_generation < 1:
        raise ValueError("invalid client generation")
    if operation not in {"BOOTSTRAP", "CAS"}:
        raise ValueError("invalid external operation")
    if operation == "BOOTSTRAP" and expected is not None:
        raise ValueError("bootstrap requires uninitialized expected state")
    if operation == "CAS" and expected is None:
        raise ValueError("CAS requires explicit expected checkpoint")
    return {
        "version": 1,
        "domain": _REQUEST_DOMAIN,
        "authority_id": authority_id,
        "client_id": client_id,
        "client_generation": client_generation,
        "request_id": request_id,
        "operation": operation,
        "expected": _checkpoint_body(expected),
        "successor": _successor_body(successor),
    }


def build_external_request(
    signing_key: Ed25519PrivateKey, *, authority_id, client_id,
    client_generation, request_id, operation, expected, successor,
):
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise TypeError("expected Ed25519 client signing key")
    body = _request_body(
        authority_id=authority_id,
        client_id=client_id,
        client_generation=client_generation,
        request_id=request_id,
        operation=operation,
        expected=expected,
        successor=successor,
    )
    payload = _canonical(body)
    if len(payload) > _MAX_MESSAGE:
        raise ValueError("external request is too large")
    return SignedExternalRequest(
        authority_id, client_id, client_generation, request_id, operation,
        expected, successor, payload, sha256(payload).hexdigest(),
        signing_key.sign(payload),
    )


def encode_request(request):
    if not isinstance(request, SignedExternalRequest):
        raise TypeError("expected signed external request")
    outer = {
        "request": json.loads(request.payload),
        "client_signature": request.client_signature.hex(),
    }
    encoded = _canonical(outer)
    if len(encoded) > _MAX_MESSAGE:
        raise ValueError("external request is too large")
    return encoded


def _successor_from_body(value):
    if not isinstance(value, dict) or set(value) != {
        "signer_id", "signature", "bound_payload",
    }:
        raise ValueError("invalid successor envelope")
    signer_id = _name(value["signer_id"], "successor signer")
    signature = bytes.fromhex(value["signature"])
    if len(signature) != 64:
        raise ValueError("invalid successor signature")
    payload = bytes.fromhex(value["bound_payload"])
    prefix = b"TENIR-EP02-SNAPSHOT-V2\\x00"
    if not payload.startswith(prefix):
        raise ValueError("invalid successor domain")
    body = json.loads(payload[len(prefix):])
    if not isinstance(body, dict) or set(body) != {"trust", "issuer_keys"}:
        raise ValueError("invalid successor payload")
    trust = body["trust"]
    if not isinstance(trust, dict) or set(trust) != {
        "revision", "policy_version", "mandates", "revoked",
    }:
        raise ValueError("invalid successor trust state")
    if not isinstance(body["issuer_keys"], list):
        raise ValueError("invalid successor issuer keys")
    if (
        type(trust["revision"]) is not int
        or trust["revision"] < 1
        or not isinstance(trust["policy_version"], str)
        or not trust["policy_version"]
        or not isinstance(trust["mandates"], list)
        or not isinstance(trust["revoked"], list)
    ):
        raise ValueError("invalid successor trust state")
    issuer_keys = tuple(
        (item[0], bytes.fromhex(item[1])) for item in body["issuer_keys"]
    )
    successor = SignedTrustState(
        TrustState(
            trust["revision"], trust["policy_version"],
            tuple(tuple(item) for item in trust["mandates"]),
            tuple(trust["revoked"]),
        ),
        signature,
        signer_id,
        issuer_keys,
    )
    if bound_snapshot_bytes(successor) != payload:
        raise ValueError("successor payload is not canonical")
    return successor


def decode_request(encoded):
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > _MAX_MESSAGE:
        raise ExternalProtocolError("invalid external request encoding")
    try:
        outer = json.loads(encoded)
        if not isinstance(outer, dict) or set(outer) != {
            "request", "client_signature",
        }:
            raise ValueError
        if encoded != _canonical(outer):
            raise ValueError
        body = outer["request"]
        expected_fields = {
            "version", "domain", "authority_id", "client_id",
            "client_generation", "request_id", "operation", "expected",
            "successor",
        }
        if not isinstance(body, dict) or set(body) != expected_fields:
            raise ValueError
        if body["version"] != 1 or body["domain"] != _REQUEST_DOMAIN:
            raise ValueError
        successor = _successor_from_body(body["successor"])
        expected = _checkpoint_from_body(body["expected"])
        rebuilt = _request_body(
            authority_id=body["authority_id"],
            client_id=body["client_id"],
            client_generation=body["client_generation"],
            request_id=body["request_id"],
            operation=body["operation"],
            expected=expected,
            successor=successor,
        )
        if rebuilt != body:
            raise ValueError
        payload = _canonical(body)
        signature = bytes.fromhex(outer["client_signature"])
        if len(signature) != 64:
            raise ValueError
        return SignedExternalRequest(
            body["authority_id"], body["client_id"],
            body["client_generation"], body["request_id"], body["operation"],
            expected, successor, payload, sha256(payload).hexdigest(), signature,
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ExternalProtocolError("invalid external request") from error


def _validate_request(request, client_keys, authority_id):
    if not isinstance(request, SignedExternalRequest):
        raise ExternalProtocolError("invalid external request")
    try:
        body = _request_body(
            authority_id=request.authority_id,
            client_id=request.client_id,
            client_generation=request.client_generation,
            request_id=request.request_id,
            operation=request.operation,
            expected=request.expected,
            successor=request.successor,
        )
        payload = _canonical(body)
        if request.authority_id != authority_id:
            raise ExternalProtocolError("authority identity mismatch")
        if request.payload != payload or request.request_digest != sha256(payload).hexdigest():
            raise ExternalProtocolError("external request payload mismatch")
        key = client_keys.get((request.client_id, request.client_generation))
        if not isinstance(key, Ed25519PublicKey):
            raise ExternalProtocolError("unknown client identity or generation")
        if not isinstance(request.client_signature, bytes):
            raise ExternalProtocolError("invalid client signature")
        key.verify(request.client_signature, payload)
    except ExternalProtocolError:
        raise
    except (InvalidSignature, TypeError, ValueError) as error:
        raise ExternalProtocolError("invalid client signature or request") from error
    return request


def _lookup_body(request, challenge):
    if not isinstance(challenge, bytes) or not 16 <= len(challenge) <= 64:
        raise ValueError("invalid lookup challenge")
    return {
        "version": 1,
        "domain": _LOOKUP_DOMAIN,
        "authority_id": request.authority_id,
        "client_id": request.client_id,
        "client_generation": request.client_generation,
        "request_id": request.request_id,
        "request_digest": request.request_digest,
        "challenge": challenge.hex(),
    }


def build_lookup_query(signing_key, *, request, challenge):
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise TypeError("expected Ed25519 client signing key")
    if not isinstance(request, SignedExternalRequest):
        raise TypeError("expected signed external request")
    payload = _canonical(_lookup_body(request, challenge))
    return SignedLookupQuery(
        request, bytes(challenge), payload, signing_key.sign(payload),
    )


def new_lookup_challenge():
    """Return a fresh 256-bit challenge for one reconciliation lookup."""
    return secrets.token_bytes(32)


def encode_lookup_query(query):
    if not isinstance(query, SignedLookupQuery):
        raise TypeError("expected signed lookup query")
    outer = {
        "request": json.loads(encode_request(query.request)),
        "lookup": json.loads(query.payload),
        "lookup_signature": query.client_signature.hex(),
    }
    encoded = _canonical(outer)
    if len(encoded) > _MAX_MESSAGE:
        raise ValueError("lookup query is too large")
    return encoded


def decode_lookup_query(encoded):
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > _MAX_MESSAGE:
        raise ExternalProtocolError("invalid lookup query encoding")
    try:
        outer = json.loads(encoded)
        if not isinstance(outer, dict) or set(outer) != {
            "request", "lookup", "lookup_signature",
        }:
            raise ValueError
        if encoded != _canonical(outer):
            raise ValueError
        request = decode_request(_canonical(outer["request"]))
        lookup = outer["lookup"]
        challenge = bytes.fromhex(lookup["challenge"])
        rebuilt = _lookup_body(request, challenge)
        if lookup != rebuilt:
            raise ValueError
        payload = _canonical(lookup)
        signature = bytes.fromhex(outer["lookup_signature"])
        if len(signature) != 64:
            raise ValueError
        return SignedLookupQuery(request, challenge, payload, signature)
    except ExternalProtocolError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ExternalProtocolError("invalid lookup query") from error


def _internal_request_id(request):
    namespace = _canonical({
        "client_id": request.client_id,
        "client_generation": request.client_generation,
        "request_id": request.request_id,
    })
    return "external:" + sha256(namespace).hexdigest()


def _receipt_body(receipt):
    return {
        "version": 1,
        "domain": _RECEIPT_DOMAIN,
        "anchor_id": receipt.anchor_id,
        "authority_id": receipt.authority_id,
        "client_id": receipt.client_id,
        "client_generation": receipt.client_generation,
        "request_id": receipt.request_id,
        "request_digest": receipt.request_digest,
        "lookup_status": receipt.lookup_status.value,
        "operation": receipt.operation,
        "result": None if receipt.result is None else receipt.result.value,
        "reason": receipt.reason,
        "observed_checkpoint": _checkpoint_body(receipt.observed_checkpoint),
        "result_checkpoint": _checkpoint_body(receipt.result_checkpoint),
        "current_checkpoint": _checkpoint_body(receipt.current_checkpoint),
        "sequence": receipt.sequence,
        "replayed": receipt.replayed,
        "challenge": None if receipt.challenge is None else receipt.challenge.hex(),
    }


def _encode_receipt(receipt):
    body = _receipt_body(receipt)
    outer = {"receipt": body, "signature": receipt.signature.hex()}
    encoded = _canonical(outer)
    if len(encoded) > _MAX_MESSAGE:
        raise ExternalProtocolError("external receipt is too large")
    return encoded


def decode_receipt(encoded):
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > _MAX_MESSAGE:
        raise ExternalProtocolError("invalid external receipt encoding")
    try:
        outer = json.loads(encoded)
        if not isinstance(outer, dict) or set(outer) != {"receipt", "signature"}:
            raise ValueError
        if encoded != _canonical(outer):
            raise ValueError
        body = outer["receipt"]
        expected = {
            "version", "domain", "anchor_id", "authority_id", "client_id",
            "client_generation", "request_id", "request_digest",
            "lookup_status", "operation", "result", "reason",
            "observed_checkpoint", "result_checkpoint", "current_checkpoint",
            "sequence", "replayed", "challenge",
        }
        if not isinstance(body, dict) or set(body) != expected:
            raise ValueError
        if body["version"] != 1 or body["domain"] != _RECEIPT_DOMAIN:
            raise ValueError
        _name(body["anchor_id"], "anchor identity")
        _name(body["authority_id"], "authority identity")
        _name(body["client_id"], "client identity")
        _name(body["request_id"], "request identity")
        if type(body["client_generation"]) is not int or body["client_generation"] < 1:
            raise ValueError
        _digest(body["request_digest"], "external request digest")
        lookup_status = LookupStatus(body["lookup_status"])
        result = None if body["result"] is None else DurableCASResult(body["result"])
        if body["operation"] not in {"BOOTSTRAP", "CAS"}:
            raise ValueError
        if body["sequence"] is not None and (
            type(body["sequence"]) is not int or body["sequence"] < 1
        ):
            raise ValueError
        if type(body["replayed"]) is not bool:
            raise ValueError
        challenge = None if body["challenge"] is None else bytes.fromhex(body["challenge"])
        if challenge is not None and not 16 <= len(challenge) <= 64:
            raise ValueError
        signature = bytes.fromhex(outer["signature"])
        if len(signature) != 64:
            raise ValueError
        receipt = SignedExternalReceipt(
            body["anchor_id"], body["authority_id"], body["client_id"],
            body["client_generation"], body["request_id"],
            body["request_digest"], lookup_status, body["operation"], result,
            body["reason"], _checkpoint_from_body(body["observed_checkpoint"]),
            _checkpoint_from_body(body["result_checkpoint"]),
            _checkpoint_from_body(body["current_checkpoint"]),
            body["sequence"], body["replayed"], challenge, signature,
        )
        if _receipt_body(receipt) != body:
            raise ValueError
        if lookup_status == LookupStatus.NOT_RECORDED and any((
            result is not None,
            body["reason"] is not None,
            body["sequence"] is not None,
            body["observed_checkpoint"] is not None,
            body["result_checkpoint"] is not None,
            body["replayed"],
        )):
            raise ValueError
        return receipt
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ExternalProtocolError("invalid external receipt") from error


def verify_receipt(
    receipt, *, request, trusted_anchors: Mapping[str, Ed25519PublicKey],
    challenge=None,
):
    if not isinstance(receipt, SignedExternalReceipt):
        return False
    try:
        if not isinstance(request, SignedExternalRequest):
            return False
        body = _request_body(
            authority_id=request.authority_id,
            client_id=request.client_id,
            client_generation=request.client_generation,
            request_id=request.request_id,
            operation=request.operation,
            expected=request.expected,
            successor=request.successor,
        )
        payload = _canonical(body)
        if (
            request.payload != payload
            or request.request_digest != sha256(payload).hexdigest()
        ):
            return False
    except (TypeError, ValueError):
        return False
    if (
        receipt.authority_id != request.authority_id
        or receipt.client_id != request.client_id
        or receipt.client_generation != request.client_generation
        or receipt.request_id != request.request_id
        or receipt.request_digest != request.request_digest
        or receipt.operation != request.operation
        or receipt.challenge != challenge
    ):
        return False
    key = trusted_anchors.get(receipt.anchor_id)
    if not isinstance(key, Ed25519PublicKey):
        return False
    try:
        key.verify(receipt.signature, _canonical(_receipt_body(receipt)))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


class ExternalAuthorityService:
    """Authenticated protocol endpoint around one durable monotone authority."""

    def __init__(
        self, store: DurableAnchorCAS, *, anchor_id: str,
        signing_key: Ed25519PrivateKey,
        client_keys: Mapping[tuple[str, int], Ed25519PublicKey],
    ):
        if not isinstance(store, DurableAnchorCAS):
            raise TypeError("expected DurableAnchorCAS")
        _name(anchor_id, "anchor identity")
        if not isinstance(signing_key, Ed25519PrivateKey):
            raise TypeError("expected Ed25519 anchor signing key")
        keys = {}
        for identity, key in client_keys.items():
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or not isinstance(key, Ed25519PublicKey)
            ):
                raise ValueError("invalid client key profile")
            client_id, generation = identity
            _name(client_id, "client identity")
            if type(generation) is not int or generation < 1:
                raise ValueError("invalid client generation")
            keys[(client_id, generation)] = key
        if not keys:
            raise ValueError("at least one client key is required")
        self.store = store
        self.anchor_id = anchor_id
        self._signing_key = signing_key
        self._client_keys = keys

    def _validate_request(self, request):
        return _validate_request(
            request, self._client_keys, self.store.authority_id,
        )

    def _receipt(self, request, response, *, status, challenge):
        if response is None:
            values = (None, None, None, None, self.store.get(), None, False)
        else:
            values = (
                response.result, response.reason, response.observed_checkpoint,
                response.result_checkpoint, response.current_checkpoint,
                response.sequence, response.replayed,
            )
        unsigned = SignedExternalReceipt(
            self.anchor_id, self.store.authority_id, request.client_id,
            request.client_generation, request.request_id,
            request.request_digest, status, request.operation,
            *values, challenge, b"",
        )
        signature = self._signing_key.sign(_canonical(_receipt_body(unsigned)))
        return _encode_receipt(SignedExternalReceipt(
            unsigned.anchor_id, unsigned.authority_id, unsigned.client_id,
            unsigned.client_generation, unsigned.request_id,
            unsigned.request_digest, unsigned.lookup_status, unsigned.operation,
            unsigned.result, unsigned.reason, unsigned.observed_checkpoint,
            unsigned.result_checkpoint, unsigned.current_checkpoint,
            unsigned.sequence, unsigned.replayed, unsigned.challenge, signature,
        ))

    def execute(self, request):
        if isinstance(request, bytes):
            request = decode_request(request)
        request = self._validate_request(request)
        internal_id = _internal_request_id(request)
        try:
            if request.operation == "BOOTSTRAP":
                response = self.store.bootstrap(internal_id, request.successor)
            else:
                response = self.store.compare_and_swap(
                    internal_id, request.expected, request.successor,
                )
        except (TypeError, ValueError) as error:
            raise ExternalProtocolError("invalid external CAS context") from error
        return self._receipt(
            request, response, status=LookupStatus.RECORDED, challenge=None,
        )

    def lookup(self, query):
        if isinstance(query, bytes):
            query = decode_lookup_query(query)
        if not isinstance(query, SignedLookupQuery):
            raise ExternalProtocolError("invalid lookup query")
        request = self._validate_request(query.request)
        try:
            payload = _canonical(_lookup_body(request, query.challenge))
            if query.payload != payload:
                raise ExternalProtocolError("lookup payload mismatch")
            key = self._client_keys[(request.client_id, request.client_generation)]
            key.verify(query.client_signature, payload)
        except ExternalProtocolError:
            raise
        except (InvalidSignature, TypeError, ValueError, KeyError) as error:
            raise ExternalProtocolError("invalid lookup signature") from error
        if request.operation == "BOOTSTRAP":
            store_digest = self.store.bootstrap_request_digest(request.successor)
        else:
            store_digest = self.store.cas_request_digest(
                request.expected, request.successor,
            )
        observation = self.store.lookup(
            _internal_request_id(request), store_digest,
        )
        return self._receipt(
            request, observation.response,
            status=observation.status,
            challenge=query.challenge,
        )


class ExternalCASClient:
    """Fail-closed client over an injected request/response transport.

    The transport object exposes ``execute(bytes)`` and ``lookup(bytes)``.
    Socket timeouts and connection failures become ``ExternalOutcomeUnknown``;
    they never become a terminal failure or an automatic second request.
    """

    def __init__(self, transport, *, trusted_anchors):
        if not isinstance(trusted_anchors, Mapping) or not trusted_anchors:
            raise ValueError("at least one trusted anchor key is required")
        self.transport = transport
        self.trusted_anchors = dict(trusted_anchors)

    @staticmethod
    def _call(call, payload):
        try:
            return call(payload)
        except (TimeoutError, ConnectionError, OSError) as error:
            raise ExternalOutcomeUnknown("external authority outcome unknown") from error

    def execute(self, request):
        encoded = self._call(self.transport.execute, encode_request(request))
        receipt = decode_receipt(encoded)
        if not verify_receipt(
            receipt, request=request, trusted_anchors=self.trusted_anchors,
        ):
            raise ExternalProtocolError("external receipt authentication failed")
        return receipt

    def lookup(self, query):
        encoded = self._call(self.transport.lookup, encode_lookup_query(query))
        receipt = decode_receipt(encoded)
        if not verify_receipt(
            receipt, request=query.request, challenge=query.challenge,
            trusted_anchors=self.trusted_anchors,
        ):
            raise ExternalProtocolError("lookup receipt authentication failed")
        return receipt
