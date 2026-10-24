"""Experimental single-host EP-02 commit authorization authority.

This module deliberately remains detached from the governor.  It adds an
operation-bound, request-idempotent authorization and single-consumption
ledger to the same SQLite database used by :mod:`ep02_durable_cas`.  Generic
CAS or preflight receipts are not dispatch authorizations.

The implementation is Profile S only: one controlled host, SQLite WAL and
multiple local processes.  It does not provide network transport, consensus,
external anti-rollback or atomicity with provider effects.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
import re
import sqlite3
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tenir_governance.experimental.ep01_evidence import SignedClaim
from tenir_governance.experimental.ep01_policy import Verdict
from tenir_governance.experimental.ep02_binding import BoundFinding
from tenir_governance.experimental.ep02_durable_cas import (
    DurableAnchorCAS,
    StoreIntegrityError,
)
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
    revalidate_bound,
)


SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TEXT = 256
_MAX_ENVELOPE_BYTES = 32_768
_BINDING_FIELDS = {
    "authority_id", "profile_id", "profile_generation",
    "institutional_scope", "operation_id", "idempotency_key",
    "passage_id", "agent_id", "action_type", "resource_id",
    "amount", "payload_digest", "claim_digest", "prior_document",
    "evidence_scope", "policy_version", "trust_revision",
    "trust_digest", "signed_snapshot_digest",
}


class AuthorizationDecision(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"


class ConsumptionDecision(str, Enum):
    CONSUMED = "CONSUMED"
    STALE = "STALE"
    REJECTED = "REJECTED"


class CommitLookupStatus(str, Enum):
    RECORDED = "RECORDED"
    NOT_RECORDED = "NOT_RECORDED"


class AuthorizationRequestConflict(RuntimeError):
    """A request ID was reused with different immutable operation context."""


class AuthorizationConsumptionConflict(RuntimeError):
    """An authorization was already consumed by another dispatch claim."""


@dataclass(frozen=True)
class OperationBinding:
    authority_id: str
    profile_id: str
    profile_generation: int
    institutional_scope: str
    operation_id: str
    idempotency_key: str
    passage_id: str
    agent_id: str
    action_type: str
    resource_id: str
    amount: str
    payload_digest: str
    claim_digest: str
    prior_document: str
    evidence_scope: str
    policy_version: str
    trust_revision: int
    trust_digest: str
    signed_snapshot_digest: str


@dataclass(frozen=True)
class CommitAuthorizationRequest:
    request_id: str
    binding: OperationBinding


@dataclass(frozen=True)
class CommitAuthorizationReceipt:
    authority_id: str
    profile_id: str
    profile_generation: int
    receipt_key_id: str
    receipt_key_generation: int
    request_id: str
    request_digest: str
    binding_digest: str
    decision: AuthorizationDecision
    reason: str
    trust_checkpoint: TrustCheckpoint | None
    sequence: int
    signature: bytes
    replayed: bool = False


@dataclass(frozen=True)
class CommitConsumptionReceipt:
    authority_id: str
    profile_id: str
    profile_generation: int
    receipt_key_id: str
    receipt_key_generation: int
    request_id: str
    request_digest: str
    binding_digest: str
    authorization_sequence: int
    dispatch_claim_id: str
    decision: ConsumptionDecision
    reason: str
    trust_checkpoint: TrustCheckpoint | None
    sequence: int
    signature: bytes
    replayed: bool = False


@dataclass(frozen=True)
class AuthorizationLookup:
    status: CommitLookupStatus
    receipt: CommitAuthorizationReceipt | None


@dataclass(frozen=True)
class ConsumptionLookup:
    status: CommitLookupStatus
    receipt: CommitConsumptionReceipt | None


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _name(value, label, *, empty=False):
    if empty and value == "":
        return ""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _text(value, label):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_TEXT
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"invalid {label}")
    return value


def _digest(value, label):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def canonical_amount(value) -> str:
    """Return a unique finite non-negative decimal representation."""
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid amount") from None
    if not number.is_finite() or number < 0:
        raise ValueError("invalid amount")
    if number == 0:
        return "0"
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if len(normalized) > 128:
        raise ValueError("amount is too large")
    return normalized


def operation_binding(
    *, authority_id, profile_id, profile_generation, institutional_scope,
    operation_id, idempotency_key, passage_id, agent_id, action_type,
    resource_id, amount, payload_digest, finding: BoundFinding,
) -> OperationBinding:
    """Build and validate a complete immutable operation binding."""
    if not isinstance(finding, BoundFinding):
        raise TypeError("expected BoundFinding")
    if type(profile_generation) is not int or profile_generation < 1:
        raise ValueError("invalid profile generation")
    if type(finding.trust_revision) is not int or finding.trust_revision < 1:
        raise ValueError("invalid trust revision")
    return _validate_binding(OperationBinding(
        authority_id=_name(authority_id, "authority identity"),
        profile_id=_name(profile_id, "profile identity"),
        profile_generation=profile_generation,
        institutional_scope=_name(institutional_scope, "institutional scope"),
        operation_id=_name(operation_id, "operation identity"),
        idempotency_key=_name(idempotency_key, "idempotency key"),
        passage_id=_name(passage_id, "passage identity"),
        agent_id=_name(agent_id, "agent identity"),
        action_type=_text(action_type, "action type"),
        resource_id=_name(resource_id, "resource identity", empty=True),
        amount=canonical_amount(amount),
        payload_digest=_digest(payload_digest, "payload digest"),
        claim_digest=_digest(finding.claim_digest, "claim digest"),
        prior_document=_text(finding.prior_document, "prior document"),
        evidence_scope=_text(finding.scope, "evidence scope"),
        policy_version=_text(finding.policy_version, "policy version"),
        trust_revision=finding.trust_revision,
        trust_digest=_digest(finding.trust_digest, "trust digest"),
        signed_snapshot_digest=_digest(
            finding.signed_snapshot_digest, "signed snapshot digest",
        ),
    ))


def _validate_binding(value):
    if not isinstance(value, OperationBinding):
        raise TypeError("expected OperationBinding")
    _name(value.authority_id, "authority identity")
    _name(value.profile_id, "profile identity")
    if type(value.profile_generation) is not int or value.profile_generation < 1:
        raise ValueError("invalid profile generation")
    _name(value.institutional_scope, "institutional scope")
    _name(value.operation_id, "operation identity")
    _name(value.idempotency_key, "idempotency key")
    _name(value.passage_id, "passage identity")
    _name(value.agent_id, "agent identity")
    _text(value.action_type, "action type")
    _name(value.resource_id, "resource identity", empty=True)
    if value.amount != canonical_amount(value.amount):
        raise ValueError("noncanonical amount")
    _digest(value.payload_digest, "payload digest")
    _digest(value.claim_digest, "claim digest")
    _text(value.prior_document, "prior document")
    _text(value.evidence_scope, "evidence scope")
    _text(value.policy_version, "policy version")
    if type(value.trust_revision) is not int or value.trust_revision < 1:
        raise ValueError("invalid trust revision")
    _digest(value.trust_digest, "trust digest")
    _digest(value.signed_snapshot_digest, "signed snapshot digest")
    return value


def _binding_body(binding):
    _validate_binding(binding)
    return {
        "authority_id": binding.authority_id,
        "profile_id": binding.profile_id,
        "profile_generation": binding.profile_generation,
        "institutional_scope": binding.institutional_scope,
        "operation_id": binding.operation_id,
        "idempotency_key": binding.idempotency_key,
        "passage_id": binding.passage_id,
        "agent_id": binding.agent_id,
        "action_type": binding.action_type,
        "resource_id": binding.resource_id,
        "amount": binding.amount,
        "payload_digest": binding.payload_digest,
        "claim_digest": binding.claim_digest,
        "prior_document": binding.prior_document,
        "evidence_scope": binding.evidence_scope,
        "policy_version": binding.policy_version,
        "trust_revision": binding.trust_revision,
        "trust_digest": binding.trust_digest,
        "signed_snapshot_digest": binding.signed_snapshot_digest,
    }


def _binding_from_body(body):
    if not isinstance(body, dict) or set(body) != _BINDING_FIELDS:
        raise ValueError("invalid operation binding fields")
    return _validate_binding(OperationBinding(**body))


def _request_context(request):
    if not isinstance(request, CommitAuthorizationRequest):
        raise TypeError("expected CommitAuthorizationRequest")
    request_id = _name(request.request_id, "authorization request identity")
    binding = _validate_binding(request.binding)
    envelope = {
        "version": 1,
        "domain": "TENIR-EP02-PROFILE-S-COMMIT-AUTHORIZATION",
        "request_id": request_id,
        "binding": _binding_body(binding),
    }
    encoded = _canonical(envelope)
    if len(encoded) > _MAX_ENVELOPE_BYTES:
        raise ValueError("authorization request is too large")
    return (
        envelope,
        encoded.decode("utf-8"),
        sha256(encoded).hexdigest(),
        sha256(_canonical(envelope["binding"])).hexdigest(),
    )


def _checkpoint_body(checkpoint):
    if checkpoint is None:
        return None
    return {
        "revision": checkpoint.revision,
        "fingerprint": checkpoint.snapshot_fingerprint,
    }


def _checkpoint_from_body(body):
    if body is None:
        return None
    if not isinstance(body, dict) or set(body) != {"revision", "fingerprint"}:
        raise ValueError("invalid checkpoint")
    if type(body["revision"]) is not int or body["revision"] < 1:
        raise ValueError("invalid checkpoint revision")
    return TrustCheckpoint(
        body["revision"], _digest(body["fingerprint"], "checkpoint fingerprint"),
    )


def _authorization_body(receipt):
    return {
        "version": 1,
        "domain": "TENIR-EP02-PROFILE-S-COMMIT-RECEIPT",
        "authority_id": receipt.authority_id,
        "profile_id": receipt.profile_id,
        "profile_generation": receipt.profile_generation,
        "receipt_key_id": receipt.receipt_key_id,
        "receipt_key_generation": receipt.receipt_key_generation,
        "request_id": receipt.request_id,
        "request_digest": receipt.request_digest,
        "binding_digest": receipt.binding_digest,
        "decision": receipt.decision.value,
        "reason": receipt.reason,
        "trust_checkpoint": _checkpoint_body(receipt.trust_checkpoint),
        "sequence": receipt.sequence,
    }


def _consumption_body(receipt):
    return {
        "version": 1,
        "domain": "TENIR-EP02-PROFILE-S-CONSUMPTION-RECEIPT",
        "authority_id": receipt.authority_id,
        "profile_id": receipt.profile_id,
        "profile_generation": receipt.profile_generation,
        "receipt_key_id": receipt.receipt_key_id,
        "receipt_key_generation": receipt.receipt_key_generation,
        "request_id": receipt.request_id,
        "request_digest": receipt.request_digest,
        "binding_digest": receipt.binding_digest,
        "authorization_sequence": receipt.authorization_sequence,
        "dispatch_claim_id": receipt.dispatch_claim_id,
        "decision": receipt.decision.value,
        "reason": receipt.reason,
        "trust_checkpoint": _checkpoint_body(receipt.trust_checkpoint),
        "sequence": receipt.sequence,
    }


def verify_authorization_receipt(receipt, request, *, trusted_keys):
    try:
        if not isinstance(receipt, CommitAuthorizationReceipt):
            return False
        _, _, request_digest, binding_digest = _request_context(request)
        key = trusted_keys.get(
            (receipt.receipt_key_id, receipt.receipt_key_generation),
        )
        if not isinstance(key, Ed25519PublicKey):
            return False
        if (
            receipt.authority_id != request.binding.authority_id
            or receipt.profile_id != request.binding.profile_id
            or receipt.profile_generation != request.binding.profile_generation
            or receipt.request_id != request.request_id
            or receipt.request_digest != request_digest
            or receipt.binding_digest != binding_digest
            or type(receipt.sequence) is not int
            or receipt.sequence < 1
            or not isinstance(receipt.signature, bytes)
            or len(receipt.signature) != 64
        ):
            return False
        key.verify(receipt.signature, _canonical(_authorization_body(receipt)))
        return True
    except (InvalidSignature, ValueError, TypeError, AttributeError):
        return False


def verify_consumption_receipt(receipt, request, authorization, *, trusted_keys):
    try:
        if not isinstance(receipt, CommitConsumptionReceipt):
            return False
        if not verify_authorization_receipt(
            authorization, request, trusted_keys=trusted_keys,
        ):
            return False
        key = trusted_keys.get(
            (receipt.receipt_key_id, receipt.receipt_key_generation),
        )
        if not isinstance(key, Ed25519PublicKey):
            return False
        if (
            receipt.authority_id != authorization.authority_id
            or receipt.profile_id != authorization.profile_id
            or receipt.profile_generation != authorization.profile_generation
            or receipt.request_id != authorization.request_id
            or receipt.request_digest != authorization.request_digest
            or receipt.binding_digest != authorization.binding_digest
            or receipt.authorization_sequence != authorization.sequence
            or type(receipt.sequence) is not int
            or receipt.sequence < 1
            or not isinstance(receipt.signature, bytes)
            or len(receipt.signature) != 64
        ):
            return False
        key.verify(receipt.signature, _canonical(_consumption_body(receipt)))
        return True
    except (InvalidSignature, ValueError, TypeError, AttributeError):
        return False


class ProfileSCommitAuthority:
    """Operation authorization ledger colocated with a durable anchor CAS."""

    def __init__(self, anchor, signing_key, key_id, key_generation):
        self.anchor = anchor
        self._signing_key = signing_key
        self.receipt_key_id = key_id
        self.receipt_key_generation = key_generation
        self._public_key = signing_key.public_key()

    @classmethod
    def provision(
        cls, path, *, authority_id, trust_profile_id, trust_generation, roots,
        receipt_key_id, receipt_key_generation, receipt_signing_key,
    ):
        if not isinstance(receipt_signing_key, Ed25519PrivateKey):
            raise ValueError("invalid receipt signing key")
        _name(receipt_key_id, "receipt key identity")
        if type(receipt_key_generation) is not int or receipt_key_generation < 1:
            raise ValueError("invalid receipt key generation")
        anchor = DurableAnchorCAS.provision(
            path,
            authority_id=authority_id,
            trust_profile_id=trust_profile_id,
            trust_generation=trust_generation,
            roots=roots,
        )
        raw = receipt_signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        connection = anchor._connect()
        try:
            connection.executescript("""
                CREATE TABLE commit_profile (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    receipt_key_id TEXT NOT NULL,
                    receipt_key_generation INTEGER NOT NULL,
                    receipt_public_key TEXT NOT NULL
                );
                CREATE TABLE commit_authorizations (
                    sequence INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    binding_digest TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    receipt_body_json TEXT NOT NULL,
                    receipt_signature TEXT NOT NULL
                );
                CREATE TABLE commit_consumptions (
                    sequence INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    binding_digest TEXT NOT NULL,
                    dispatch_claim_id TEXT NOT NULL UNIQUE,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    receipt_body_json TEXT NOT NULL,
                    receipt_signature TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES commit_authorizations(request_id)
                );
                CREATE TRIGGER commit_profile_no_update
                BEFORE UPDATE ON commit_profile
                BEGIN SELECT RAISE(ABORT, 'commit profile is immutable'); END;
                CREATE TRIGGER commit_profile_no_delete
                BEFORE DELETE ON commit_profile
                BEGIN SELECT RAISE(ABORT, 'commit profile is immutable'); END;
                CREATE TRIGGER commit_authorizations_no_update
                BEFORE UPDATE ON commit_authorizations
                BEGIN SELECT RAISE(ABORT, 'authorization rows are immutable'); END;
                CREATE TRIGGER commit_authorizations_no_delete
                BEFORE DELETE ON commit_authorizations
                BEGIN SELECT RAISE(ABORT, 'authorization rows are immutable'); END;
                CREATE TRIGGER commit_consumptions_no_update
                BEFORE UPDATE ON commit_consumptions
                BEGIN SELECT RAISE(ABORT, 'consumption rows are immutable'); END;
                CREATE TRIGGER commit_consumptions_no_delete
                BEFORE DELETE ON commit_consumptions
                BEGIN SELECT RAISE(ABORT, 'consumption rows are immutable'); END;
            """)
            connection.execute(
                "INSERT INTO commit_profile VALUES (1, ?, ?, ?, ?)",
                (SCHEMA_VERSION, receipt_key_id, receipt_key_generation, raw.hex()),
            )
            connection.commit()
        finally:
            connection.close()
        return cls.open(path, receipt_signing_key=receipt_signing_key)

    @classmethod
    def open(cls, path, *, receipt_signing_key):
        if not isinstance(receipt_signing_key, Ed25519PrivateKey):
            raise ValueError("invalid receipt signing key")
        anchor = DurableAnchorCAS.open(path)
        connection = anchor._connect()
        try:
            row = connection.execute(
                "SELECT schema_version, receipt_key_id, receipt_key_generation, "
                "receipt_public_key FROM commit_profile WHERE singleton = 1"
            ).fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise StoreIntegrityError("missing or unsupported commit schema")
            _, key_id, generation, raw_hex = row
            _name(key_id, "stored receipt key identity")
            if type(generation) is not int or generation < 1:
                raise StoreIntegrityError("invalid stored receipt key generation")
            raw = bytes.fromhex(raw_hex)
            actual = receipt_signing_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if len(raw) != 32 or raw != actual or raw_hex != raw.hex():
                raise StoreIntegrityError("receipt signing key does not match profile")
            cls._validate_guards(connection)
            service = cls(anchor, receipt_signing_key, key_id, generation)
            service._validate_rows(connection)
            return service
        except StoreIntegrityError:
            raise
        except (ValueError, TypeError, sqlite3.Error) as error:
            raise StoreIntegrityError("commit authority validation failed") from error
        finally:
            connection.close()

    @staticmethod
    def _validate_guards(connection):
        required = {
            "commit_profile_no_update", "commit_profile_no_delete",
            "commit_authorizations_no_update", "commit_authorizations_no_delete",
            "commit_consumptions_no_update", "commit_consumptions_no_delete",
        }
        actual = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        if not required.issubset(actual):
            raise StoreIntegrityError("commit immutable-row guards are missing")

    def _trusted_keys(self):
        return {(self.receipt_key_id, self.receipt_key_generation): self._public_key}

    def _connect(self):
        connection = self.anchor._connect()
        try:
            row = connection.execute(
                "SELECT schema_version, receipt_key_id, receipt_key_generation, "
                "receipt_public_key FROM commit_profile WHERE singleton = 1"
            ).fetchone()
            expected_raw = self._public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ).hex()
            if row != (
                SCHEMA_VERSION, self.receipt_key_id,
                self.receipt_key_generation, expected_raw,
            ):
                raise StoreIntegrityError("commit authority identity changed after open")
            self._validate_guards(connection)
            return connection
        except Exception:
            connection.close()
            raise

    def _validate_rows(self, connection):
        requests = {}
        for row in connection.execute(
            "SELECT sequence, request_id, request_digest, binding_digest, "
            "envelope_json, decision, reason, receipt_body_json, "
            "receipt_signature FROM commit_authorizations ORDER BY sequence"
        ):
            try:
                sequence, request_id, request_digest, binding_digest = row[:4]
                envelope_json, decision, reason, body_json, signature_hex = row[4:]
                envelope = json.loads(envelope_json)
                if _canonical(envelope).decode("utf-8") != envelope_json:
                    raise ValueError
                binding = _binding_from_body(envelope["binding"])
                request = CommitAuthorizationRequest(envelope["request_id"], binding)
                _, expected_json, expected_digest, expected_binding = _request_context(request)
                if (
                    type(sequence) is not int or sequence < 1
                    or request_id != request.request_id
                    or envelope_json != expected_json
                    or request_digest != expected_digest
                    or binding_digest != expected_binding
                ):
                    raise ValueError
                body = json.loads(body_json)
                receipt = self._authorization_from_body(
                    body, bytes.fromhex(signature_hex), replayed=False,
                )
                if (
                    _canonical(body).decode("utf-8") != body_json
                    or receipt.sequence != sequence
                    or receipt.request_id != request_id
                    or receipt.decision.value != decision
                    or receipt.reason != reason
                    or not verify_authorization_receipt(
                        receipt, request, trusted_keys=self._trusted_keys(),
                    )
                ):
                    raise ValueError
                requests[request_id] = (request, receipt)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                raise StoreIntegrityError("invalid commit authorization row") from error

        for row in connection.execute(
            "SELECT sequence, request_id, request_digest, binding_digest, "
            "dispatch_claim_id, decision, reason, receipt_body_json, "
            "receipt_signature FROM commit_consumptions ORDER BY sequence"
        ):
            try:
                sequence, request_id, request_digest, binding_digest = row[:4]
                claim_id, decision, reason, body_json, signature_hex = row[4:]
                request, authorization = requests[request_id]
                body = json.loads(body_json)
                receipt = self._consumption_from_body(
                    body, bytes.fromhex(signature_hex), replayed=False,
                )
                if (
                    _canonical(body).decode("utf-8") != body_json
                    or receipt.sequence != sequence
                    or receipt.request_id != request_id
                    or receipt.request_digest != request_digest
                    or receipt.binding_digest != binding_digest
                    or receipt.dispatch_claim_id != claim_id
                    or receipt.decision.value != decision
                    or receipt.reason != reason
                    or not verify_consumption_receipt(
                        receipt, request, authorization,
                        trusted_keys=self._trusted_keys(),
                    )
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                raise StoreIntegrityError("invalid commit consumption row") from error

    def _authorization_from_body(self, body, signature, *, replayed):
        expected = {
            "version", "domain", "authority_id", "profile_id",
            "profile_generation", "receipt_key_id", "receipt_key_generation",
            "request_id", "request_digest", "binding_digest", "decision",
            "reason", "trust_checkpoint", "sequence",
        }
        if not isinstance(body, dict) or set(body) != expected:
            raise ValueError("invalid authorization receipt")
        if body["version"] != 1 or body["domain"] != "TENIR-EP02-PROFILE-S-COMMIT-RECEIPT":
            raise ValueError("invalid authorization receipt domain")
        return CommitAuthorizationReceipt(
            authority_id=body["authority_id"], profile_id=body["profile_id"],
            profile_generation=body["profile_generation"],
            receipt_key_id=body["receipt_key_id"],
            receipt_key_generation=body["receipt_key_generation"],
            request_id=body["request_id"], request_digest=body["request_digest"],
            binding_digest=body["binding_digest"],
            decision=AuthorizationDecision(body["decision"]),
            reason=body["reason"],
            trust_checkpoint=_checkpoint_from_body(body["trust_checkpoint"]),
            sequence=body["sequence"], signature=signature, replayed=replayed,
        )

    def _consumption_from_body(self, body, signature, *, replayed):
        expected = {
            "version", "domain", "authority_id", "profile_id",
            "profile_generation", "receipt_key_id", "receipt_key_generation",
            "request_id", "request_digest", "binding_digest",
            "authorization_sequence", "dispatch_claim_id", "decision",
            "reason", "trust_checkpoint", "sequence",
        }
        if not isinstance(body, dict) or set(body) != expected:
            raise ValueError("invalid consumption receipt")
        if body["version"] != 1 or body["domain"] != "TENIR-EP02-PROFILE-S-CONSUMPTION-RECEIPT":
            raise ValueError("invalid consumption receipt domain")
        return CommitConsumptionReceipt(
            authority_id=body["authority_id"], profile_id=body["profile_id"],
            profile_generation=body["profile_generation"],
            receipt_key_id=body["receipt_key_id"],
            receipt_key_generation=body["receipt_key_generation"],
            request_id=body["request_id"], request_digest=body["request_digest"],
            binding_digest=body["binding_digest"],
            authorization_sequence=body["authorization_sequence"],
            dispatch_claim_id=body["dispatch_claim_id"],
            decision=ConsumptionDecision(body["decision"]),
            reason=body["reason"],
            trust_checkpoint=_checkpoint_from_body(body["trust_checkpoint"]),
            sequence=body["sequence"], signature=signature, replayed=replayed,
        )

    def authorize(self, request, *, finding, claim, current_snapshot, now):
        _, envelope_json, request_digest, binding_digest = _request_context(request)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._fault("after_authorization_begin")
            row = connection.execute(
                "SELECT receipt_body_json, receipt_signature, request_digest "
                "FROM commit_authorizations WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is not None:
                if row[2] != request_digest:
                    raise AuthorizationRequestConflict(
                        "authorization request ID has different immutable context"
                    )
                receipt = self._authorization_from_body(
                    json.loads(row[0]), bytes.fromhex(row[1]), replayed=True,
                )
                connection.commit()
                return receipt

            current = self.anchor._read_checkpoint(connection)
            decision, reason = self._evaluate(
                request, finding=finding, claim=claim,
                current_snapshot=current_snapshot, now=now, current=current,
            )
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM commit_authorizations"
            ).fetchone()[0]
            unsigned = CommitAuthorizationReceipt(
                authority_id=self.anchor.authority_id,
                profile_id=self.anchor.trust_profile_id,
                profile_generation=self.anchor.trust_generation,
                receipt_key_id=self.receipt_key_id,
                receipt_key_generation=self.receipt_key_generation,
                request_id=request.request_id,
                request_digest=request_digest,
                binding_digest=binding_digest,
                decision=decision,
                reason=reason,
                trust_checkpoint=current,
                sequence=sequence,
                signature=b"",
            )
            body = _authorization_body(unsigned)
            body_json = _canonical(body).decode("utf-8")
            signature = self._signing_key.sign(body_json.encode("utf-8"))
            connection.execute(
                "INSERT INTO commit_authorizations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sequence, request.request_id, request_digest, binding_digest,
                 envelope_json, decision.value, reason, body_json, signature.hex()),
            )
            self._fault("after_authorization_insert")
            self._fault("before_authorization_commit")
            connection.commit()
            self._fault("after_authorization_commit_before_reply")
            return replace(unsigned, signature=signature)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _evaluate(self, request, *, finding, claim, current_snapshot, now, current):
        binding = request.binding
        if (
            binding.authority_id != self.anchor.authority_id
            or binding.profile_id != self.anchor.trust_profile_id
            or binding.profile_generation != self.anchor.trust_generation
        ):
            return AuthorizationDecision.REJECTED, "AUTHORITY_BINDING_MISMATCH"
        if current is None:
            return AuthorizationDecision.REJECTED, "TRUST_NOT_INITIALIZED"
        expected = TrustCheckpoint(
            binding.trust_revision, binding.signed_snapshot_digest,
        )
        if current != expected:
            return AuthorizationDecision.REJECTED, "TRUST_CHECKPOINT_STALE"
        if not isinstance(finding, BoundFinding) or (
            finding.trust_revision != binding.trust_revision
            or finding.trust_digest != binding.trust_digest
            or finding.policy_version != binding.policy_version
            or finding.claim_digest != binding.claim_digest
            or finding.prior_document != binding.prior_document
            or finding.scope != binding.evidence_scope
            or finding.signed_snapshot_digest != binding.signed_snapshot_digest
        ):
            return AuthorizationDecision.REJECTED, "PREFLIGHT_BINDING_MISMATCH"
        if not isinstance(current_snapshot, SignedTrustState):
            return AuthorizationDecision.REJECTED, "TRUST_SNAPSHOT_MALFORMED"
        try:
            fingerprint = sha256(bound_snapshot_bytes(current_snapshot)).hexdigest()
        except (ValueError, TypeError, AttributeError):
            return AuthorizationDecision.REJECTED, "TRUST_SNAPSHOT_MALFORMED"
        if (
            current_snapshot.state.revision != binding.trust_revision
            or current_snapshot.state.digest() != binding.trust_digest
            or current_snapshot.state.policy_version != binding.policy_version
            or fingerprint != binding.signed_snapshot_digest
        ):
            return AuthorizationDecision.REJECTED, "TRUST_SNAPSHOT_MISMATCH"
        try:
            verdict, evidence_reason = revalidate_bound(
                finding, claim, prior=binding.prior_document,
                scope=binding.evidence_scope, now=now,
                current=current_snapshot, root_keys=self.anchor._roots,
                minimum_revision=current.revision,
            )
        except (ValueError, TypeError, AttributeError):
            return AuthorizationDecision.REJECTED, "EVIDENCE_REVALIDATION_ERROR"
        if verdict != Verdict.ADMISSIBLE:
            return AuthorizationDecision.REJECTED, f"EVIDENCE_{evidence_reason}"
        return AuthorizationDecision.AUTHORIZED, "COMMIT_AUTHORIZED"

    def lookup_authorization(self, request):
        _, _, request_digest, _ = _request_context(request)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT receipt_body_json, receipt_signature, request_digest "
                "FROM commit_authorizations WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                return AuthorizationLookup(CommitLookupStatus.NOT_RECORDED, None)
            if row[2] != request_digest:
                raise AuthorizationRequestConflict(
                    "authorization request ID has different immutable context"
                )
            return AuthorizationLookup(
                CommitLookupStatus.RECORDED,
                self._authorization_from_body(
                    json.loads(row[0]), bytes.fromhex(row[1]), replayed=True,
                ),
            )
        finally:
            connection.close()

    def consume(self, request, authorization, *, dispatch_claim_id):
        _name(dispatch_claim_id, "dispatch claim identity")
        _, _, request_digest, binding_digest = _request_context(request)
        if not verify_authorization_receipt(
            authorization, request, trusted_keys=self._trusted_keys(),
        ):
            raise ValueError("authorization receipt verification failed")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._fault("after_consumption_begin")
            row = connection.execute(
                "SELECT receipt_body_json, receipt_signature, request_digest "
                "FROM commit_authorizations WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None or row[2] != request_digest:
                raise ValueError("authorization is not recorded by this authority")
            stored = self._authorization_from_body(
                json.loads(row[0]), bytes.fromhex(row[1]), replayed=False,
            )
            if (
                stored.signature != authorization.signature
                or _authorization_body(stored) != _authorization_body(authorization)
            ):
                raise ValueError("authorization receipt differs from durable record")
            existing = connection.execute(
                "SELECT receipt_body_json, receipt_signature, dispatch_claim_id "
                "FROM commit_consumptions WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if existing[2] != dispatch_claim_id:
                    raise AuthorizationConsumptionConflict(
                        "authorization already consumed by another dispatch claim"
                    )
                receipt = self._consumption_from_body(
                    json.loads(existing[0]), bytes.fromhex(existing[1]), replayed=True,
                )
                connection.commit()
                return receipt

            current = self.anchor._read_checkpoint(connection)
            if stored.decision != AuthorizationDecision.AUTHORIZED:
                decision, reason = (
                    ConsumptionDecision.REJECTED, "AUTHORIZATION_NOT_GRANTED",
                )
            elif current != stored.trust_checkpoint:
                decision, reason = (
                    ConsumptionDecision.STALE, "TRUST_CHECKPOINT_CHANGED",
                )
            else:
                decision, reason = (
                    ConsumptionDecision.CONSUMED, "DISPATCH_CLAIM_AUTHORIZED",
                )
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM commit_consumptions"
            ).fetchone()[0]
            unsigned = CommitConsumptionReceipt(
                authority_id=self.anchor.authority_id,
                profile_id=self.anchor.trust_profile_id,
                profile_generation=self.anchor.trust_generation,
                receipt_key_id=self.receipt_key_id,
                receipt_key_generation=self.receipt_key_generation,
                request_id=request.request_id,
                request_digest=request_digest,
                binding_digest=binding_digest,
                authorization_sequence=stored.sequence,
                dispatch_claim_id=dispatch_claim_id,
                decision=decision,
                reason=reason,
                trust_checkpoint=current,
                sequence=sequence,
                signature=b"",
            )
            body = _consumption_body(unsigned)
            body_json = _canonical(body).decode("utf-8")
            signature = self._signing_key.sign(body_json.encode("utf-8"))
            connection.execute(
                "INSERT INTO commit_consumptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sequence, request.request_id, request_digest, binding_digest,
                 dispatch_claim_id, decision.value, reason, body_json,
                 signature.hex()),
            )
            self._fault("after_consumption_insert")
            self._fault("before_consumption_commit")
            connection.commit()
            self._fault("after_consumption_commit_before_reply")
            return replace(unsigned, signature=signature)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def lookup_consumption(self, request):
        _, _, request_digest, _ = _request_context(request)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT receipt_body_json, receipt_signature, request_digest "
                "FROM commit_consumptions WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                return ConsumptionLookup(CommitLookupStatus.NOT_RECORDED, None)
            if row[2] != request_digest:
                raise AuthorizationRequestConflict(
                    "authorization request ID has different immutable context"
                )
            return ConsumptionLookup(
                CommitLookupStatus.RECORDED,
                self._consumption_from_body(
                    json.loads(row[0]), bytes.fromhex(row[1]), replayed=True,
                ),
            )
        finally:
            connection.close()

    def _fault(self, point):
        """No-op test seam for crash and lost-reply characterization."""
