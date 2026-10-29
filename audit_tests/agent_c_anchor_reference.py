"""Agent C reference-contract oracle; NOT a production external anchor or B2 custody.

In-memory signed high-water authority. The private signer is held only by the
reference authority object, NOT by the producer-side verifier. This is still
a single-process simulation with no independent operator, HSM or durable CAS.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from typing import Optional
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.exceptions import InvalidSignature

DOMAIN = "TENIR-AGENT-C-MONOTONE-REF-v0.1"

def _canonical(x: dict) -> bytes:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

class AnchorConflict(Exception):
    pass

@dataclass(frozen=True)
class Checkpoint:
    subject: str
    revision: int
    digest: str
    topology: str
    def as_dict(self) -> dict:
        return {"subject": self.subject, "revision": self.revision,
                "digest": self.digest, "topology": self.topology}

class ReferenceRemoteAuthority:
    """Memory-only fixture; a remote-service or non-rollback claim is forbidden."""
    def __init__(self, private_key: Ed25519PrivateKey | None = None):
        self.__private = private_key or Ed25519PrivateKey.generate()
        self.public_bytes = self.__private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(self.public_bytes).hexdigest()
        self._heads: dict[str, Checkpoint] = {}
        self._ids: dict[tuple[str, str], tuple[bytes, dict]] = {}

    def _sign(self, payload: dict) -> dict:
        return {"payload": payload, "signature": self.__private.sign(_canonical(payload)).hex()}

    def bootstrap(self, checkpoint: Checkpoint):
        if not checkpoint.subject or checkpoint.revision != 0 or len(checkpoint.digest) != 64:
            raise AnchorConflict("INVALID_BOOTSTRAP")
        if checkpoint.subject in self._heads:
            raise AnchorConflict("ALREADY_INITIALIZED")
        self._heads[checkpoint.subject] = checkpoint

    def advance(self, new: Checkpoint, *, expected_revision: int, request_id: str) -> dict:
        if not request_id or new.revision < 1 or len(new.digest) != 64:
            raise AnchorConflict("INVALID_REQUEST")
        key = (new.subject, request_id)
        request = _canonical({"new": new.as_dict(), "expected_revision": expected_revision, "request_id": request_id})
        if key in self._ids:
            prior, result = self._ids[key]
            if prior != request:
                raise AnchorConflict("IDEMPOTENCY_CONTEXT_CONFLICT")
            return result
        old = self._heads.get(new.subject)
        if old is None:
            raise AnchorConflict("UNKNOWN_SUBJECT")
        if expected_revision != old.revision or new.revision != old.revision + 1 or new.topology != old.topology:
            raise AnchorConflict("STALE_OR_CROSS_TOPOLOGY")
        self._heads[new.subject] = new
        result = self._sign({"domain": DOMAIN, "kind": "ADVANCE", "subject": new.subject,
                             "request_id": request_id, "revision": new.revision,
                             "digest": new.digest, "topology": new.topology})
        self._ids[key] = (request, result)
        return result

    def observe(self, subject: str, challenge: str) -> dict:
        if not challenge or len(challenge) < 16:
            raise AnchorConflict("INVALID_CHALLENGE")
        cp = self._heads.get(subject)
        if cp is None:
            raise AnchorConflict("UNKNOWN_SUBJECT")
        return self._sign({"domain": DOMAIN, "kind": "OBSERVATION", "challenge": challenge,
                           "checkpoint": cp.as_dict(), "signer_id": self.key_id})

class ProducerSideVerifier:
    """Accepts ONLY out-of-band pinned public key; no signer/key material."""
    def __init__(self, pinned_public_key: bytes, subject: str, topology: str):
        self.key = Ed25519PublicKey.from_public_bytes(pinned_public_key)
        self.key_id = hashlib.sha256(pinned_public_key).hexdigest()
        self.subject = subject
        self.topology = topology

    def assess(self, local: Checkpoint, observation: Optional[dict], challenge: str) -> str:
        if observation is None:
            return "HOLD_ANCHOR_UNAVAILABLE"
        try:
            payload = observation["payload"]
            self.key.verify(bytes.fromhex(observation["signature"]), _canonical(payload))
            if (payload["domain"] != DOMAIN or payload["kind"] != "OBSERVATION"
                or payload["signer_id"] != self.key_id or payload["challenge"] != challenge):
                return "HOLD_WRONG_AUTHORITY_OR_CHALLENGE"
            remote = payload["checkpoint"]
            if (remote["subject"] != self.subject or local.subject != self.subject
                or remote["topology"] != self.topology or local.topology != self.topology):
                return "HOLD_SCOPE_MISMATCH"
            if remote["revision"] > local.revision:
                return "HOLD_LOCAL_ROLLBACK"
            if remote["revision"] < local.revision:
                return "HOLD_ANCHOR_BEHIND"
            if remote["digest"] != local.digest:
                return "HOLD_FORK"
            return "MATCHED_REFERENCE_ONLY"
        except (InvalidSignature, ValueError, KeyError, TypeError, AttributeError):
            return "HOLD_INVALID_OBSERVATION"

def digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
