"""Experimental dual authorization and shared Governor quarantine.

Both signers and their configuration are trusted deployment inputs. This is a
synthetic laboratory, not a production service or a unified I8 transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from b1_approved_positive_uid_service import ApprovedPositiveUidService, canonical_approval, _FIELDS
from positive_uid_service import _identifier
from tenir_governance.composition.governor import DualKernelGovernor
from tenir_governance.objects import Passage
from tenir_governance.operations.payload_binding import canonical_payload_hash
from tenir_governance.operations.registry import OperationState
from native_positive_permit import NativePositivePermitAuthority
from b1_native_quarantine import bind_registry
from w2_2b_canonical import request_hash
from w2_integration.native_dispatch_command import snapshot_command, snapshot_payload

_RECEIPT_FIELDS = {"schema", "audience", "operation_key", "evaluation_id", "request_hash", "decision",
                   "passage_id", "expires_at", "quarantine_epoch", "policy_context_hash", "key_id"}


def _public_bytes(key):
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _check_independent(request, public_bytes):
    if not isinstance(request, dict) or set(request) != {
        "schema", "action", "operation_key", "evaluation_id", "idempotency_key", "command", "approval"
    } or request["schema"] != "tenir.positive.approved.uid-lab.v1" or request["action"] != "submit":
        raise RuntimeError("independent signed request required")
    command = snapshot_command(request["command"])
    approval = request["approval"]
    if not isinstance(approval, dict) or set(approval) != _FIELDS | {"signature"}:
        raise RuntimeError("independent approval malformed")
    body = {name: approval[name] for name in _FIELDS}
    for name in ("approval_id", "nonce", "operation_key", "evaluation_id", "idempotency_key"):
        _identifier(body[name])
    now = int(time.time())
    if (body["schema"] != "tenir.b1.approval.v1" or body["audience"] != "positive-uid-laboratory"
            or body["key_id"] != hashlib.sha256(public_bytes).hexdigest()
            or body["operation_key"] != request["operation_key"]
            or body["evaluation_id"] != request["evaluation_id"]
            or body["idempotency_key"] != request["idempotency_key"]
            or body["operation_key"] != body["idempotency_key"]
            or body["request_hash"] != request_hash(command)
            or type(body["issued_at"]) is not int or type(body["expires_at"]) is not int
            or body["issued_at"] > now + 30 or body["expires_at"] <= now
            or body["expires_at"] <= body["issued_at"] or body["expires_at"] - body["issued_at"] > 86400):
        raise RuntimeError("independent approval binding or validity refused")
    try:
        signature = approval["signature"]
        if not isinstance(signature, str) or len(signature) != 128:
            raise ValueError("bad signature")
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(bytes.fromhex(signature), canonical_approval(body))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise RuntimeError("independent approval signature refused") from exc
    return command, body


class GovernorReceiptIssuer:
    """Runs an actual Governor; it never signs a caller-supplied verdict."""

    def __init__(self, governor, signing_key, independent_public_key, *, receipt_authority,
                 policy_context, allowed_decisions=("PASS",)):
        if type(governor) is not DualKernelGovernor or not governor.registry.is_persistent:
            raise RuntimeError("actual Governor with persistent registry required")
        if not isinstance(signing_key, Ed25519PrivateKey) or not isinstance(independent_public_key, bytes) or len(independent_public_key) != 32:
            raise RuntimeError("distinct issuer and independent keys required")
        if _public_bytes(signing_key.public_key()) == independent_public_key:
            raise RuntimeError("issuer and independent keys must be distinct")
        if type(receipt_authority) is not NativePositivePermitAuthority:
            raise RuntimeError("native receipt authority required")
        allowed = {"pressure", "velocity", "capacity", "authority", "context_fit", "evidence_quality"}
        if type(policy_context) is not dict or set(policy_context) != allowed:
            raise RuntimeError("explicit trusted policy context required")
        decisions = frozenset(allowed_decisions)
        if not decisions or not decisions <= {"PASS", "FLAG"}:
            raise RuntimeError("Governor decisions must be an explicit PASS/FLAG subset")
        self.governor = governor
        self._key = signing_key
        self._independent_public = independent_public_key
        self._context = snapshot_payload(policy_context)
        self.policy_context_hash = hashlib.sha256(canonical_approval(self._context)).hexdigest()
        self._decisions = decisions
        self._receipt_authority = receipt_authority
        self._public = _public_bytes(signing_key.public_key())
        with receipt_authority._write("b1_governor_issuer_trust") as db:
            bind_registry(db, governor.registry)
            db.execute("CREATE TABLE IF NOT EXISTS b1_governor_issuer_trust "
                       "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), public_key BLOB NOT NULL)")
            row = db.execute("SELECT public_key FROM b1_governor_issuer_trust WHERE singleton=1").fetchone()
            if row is None:
                db.execute("INSERT INTO b1_governor_issuer_trust VALUES(1,?)", (self._public,))
            elif bytes(row["public_key"]) != self._public:
                raise RuntimeError("Governor issuer key changed")
            db.execute("CREATE TABLE IF NOT EXISTS b1_governor_receipts "
                       "(operation_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, receipt_json TEXT NOT NULL)")
            db.execute("CREATE TRIGGER IF NOT EXISTS b1_governor_receipts_no_update BEFORE UPDATE ON b1_governor_receipts "
                       "BEGIN SELECT RAISE(ABORT, 'Governor receipt is append-only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS b1_governor_receipts_no_delete BEFORE DELETE ON b1_governor_receipts "
                       "BEGIN SELECT RAISE(ABORT, 'Governor receipt is append-only'); END")

    def issue(self, request):
        request = snapshot_payload(request)
        command, approval = _check_independent(request, self._independent_public)
        key = request["operation_key"]
        digest = canonical_payload_hash(request)
        with self.governor.registry.quarantine_guard():
            mark = self.governor.registry._quarantine_read_locked()
            if mark.get("active") is not False:
                raise RuntimeError("shared quarantine active before Governor receipt")
            initial_epoch = mark.get("lock_id", "initial")
            record = self.governor.registry.get(key)
            with self._receipt_authority._tx() as db:
                trust = db.execute("SELECT public_key FROM b1_governor_issuer_trust WHERE singleton=1").fetchone()
                if not trust or bytes(trust["public_key"]) != self._public:
                    raise RuntimeError("Governor issuer key changed")
                previous = db.execute("SELECT request_hash, receipt_json FROM b1_governor_receipts WHERE operation_key=?", (key,)).fetchone()
            if previous:
                if not Path(self.governor.registry._store_path).is_file() or self.governor.registry.is_corrupted:
                    raise RuntimeError("Governor registry unavailable or corrupt")
                try:
                    receipt = json.loads(previous["receipt_json"])
                    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS | {"signature"}:
                        raise ValueError("receipt schema changed")
                    body = {name: value for name, value in receipt.items() if name != "signature"}
                    self._key.public_key().verify(bytes.fromhex(receipt["signature"]), canonical_approval(body))
                except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError("stored Governor receipt invalid") from exc
                if (previous["request_hash"] != digest or body.get("request_hash") != digest
                        or body.get("schema") != "tenir.b1.governor-receipt.v1"
                        or body.get("audience") != "positive-uid-laboratory"
                        or body.get("operation_key") != key
                        or body.get("evaluation_id") != request["evaluation_id"]
                        or body.get("key_id") != hashlib.sha256(self._public).hexdigest()
                        or body.get("quarantine_epoch") != mark.get("lock_id", "initial")
                        or body.get("policy_context_hash") != self.policy_context_hash
                        or body.get("decision") not in self._decisions
                        or body.get("expires_at") != approval["expires_at"]
                        or not record or record.state != OperationState.RESERVED
                        or record.payload_hash != digest or record.passage_id != body.get("passage_id")):
                    raise RuntimeError("stored Governor receipt no longer authorizes request")
                return receipt
            if record is not None:
                raise RuntimeError("existing Governor operation requires recovery")
        passage = Passage(passage_id="gov-" + key, idempotency_key=key, agent_id=command["agent_id"],
                          action_type=command["action_type"], amount=command["amount"])
        result = self.governor.adjudicate(passage, resource_id=command["resource_id"], amount=command["amount"],
                                         payload=request, **self._context)
        with self.governor.registry.quarantine_guard():
            mark = self.governor.registry._quarantine_read_locked()
            record = self.governor.registry.get(key)
            if mark.get("active") is not False:
                raise RuntimeError("shared quarantine active before Governor receipt")
            if mark.get("lock_id", "initial") != initial_epoch:
                raise RuntimeError("Governor adjudication quarantine epoch changed")
            if approval["expires_at"] <= int(time.time()):
                raise RuntimeError("independent approval expired during adjudication")
            if not Path(self.governor.registry._store_path).is_file() or self.governor.registry.is_corrupted:
                raise RuntimeError("Governor registry unavailable or corrupt")
            if (result.get("composed_decision") not in self._decisions or not record or record.state != OperationState.RESERVED
                    or record.payload_hash != canonical_payload_hash(request)):
                raise RuntimeError("Governor did not issue an allowed decision with a bound RESERVED record: "
                                   f"decision={result.get('composed_decision')}, "
                                   f"state={record.state.value if record else 'missing'}, "
                                   f"bound={bool(record and record.payload_hash == canonical_payload_hash(request))}")
            body = {"schema": "tenir.b1.governor-receipt.v1", "audience": "positive-uid-laboratory",
                    "operation_key": key, "evaluation_id": request["evaluation_id"],
                    "request_hash": canonical_payload_hash(request), "decision": result["composed_decision"],
                    "passage_id": passage.passage_id, "expires_at": approval["expires_at"],
                    "quarantine_epoch": mark.get("lock_id", "initial"),
                    "policy_context_hash": self.policy_context_hash,
                    "key_id": hashlib.sha256(_public_bytes(self._key.public_key())).hexdigest()}
            receipt = {**body, "signature": self._key.sign(canonical_approval(body)).hex()}
            with self._receipt_authority._write("b1_governor_receipt") as db:
                trust = db.execute("SELECT public_key FROM b1_governor_issuer_trust WHERE singleton=1").fetchone()
                if not trust or bytes(trust["public_key"]) != self._public:
                    raise RuntimeError("Governor issuer key changed")
                db.execute("INSERT INTO b1_governor_receipts VALUES(?,?,?)",
                           (key, digest, json.dumps(receipt, sort_keys=True, separators=(",", ":"))))
            return receipt


class DualApprovedUidService(ApprovedPositiveUidService):
    """Synthetic service requiring Governor proof, independent approval and a shared lock."""

    def __init__(self, state_dir, socket_dir, *, worker_uids, approval_public_key,
                 governor_public_key, governor_registry, policy_context_hash, allowed_decisions=("PASS",)):
        super().__init__(state_dir, socket_dir, worker_uids=worker_uids, approval_public_key=approval_public_key)
        try:
            path = Path(governor_registry._store_path or "")
            if (not path.is_absolute() or not path.is_relative_to(self.state_dir)
                    or path.resolve() != path or path.parent != self.state_dir):
                raise RuntimeError("Governor registry must be in private writer state")
            self._initialize_governor_trust(governor_public_key, governor_registry,
                                            policy_context_hash=policy_context_hash, allowed_decisions=allowed_decisions)
        except BaseException:
            self.provider.conn.close()
            raise

    def _initialize_governor_trust(self, public_bytes, registry, *, policy_context_hash, allowed_decisions=("PASS",)):
        if not isinstance(public_bytes, bytes) or len(public_bytes) != 32 or not registry.is_persistent:
            raise RuntimeError("Governor public key and persistent registry required")
        if public_bytes == self._approval_public_bytes:
            raise RuntimeError("Governor and independent keys must be distinct")
        if (not isinstance(policy_context_hash, str) or len(policy_context_hash) != 64
                or any(c not in "0123456789abcdef" for c in policy_context_hash)):
            raise RuntimeError("Governor policy context hash required")
        decisions = frozenset(allowed_decisions)
        if not decisions or not decisions <= {"PASS", "FLAG"}:
            raise RuntimeError("Governor decisions must be an explicit PASS/FLAG subset")
        decision_json = json.dumps(sorted(decisions), separators=(",", ":"))
        with self.authority._write("b1_governor_trust") as db:
            bind_registry(db, registry)
            db.execute("CREATE TABLE IF NOT EXISTS b1_governor_trust "
                       "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), public_key BLOB NOT NULL, decisions TEXT NOT NULL, policy_context_hash TEXT NOT NULL)")
            row = db.execute("SELECT public_key, decisions, policy_context_hash FROM b1_governor_trust WHERE singleton=1").fetchone()
            if row is None:
                if db.execute("SELECT 1 FROM w2_positive_v1_permits LIMIT 1").fetchone():
                    raise RuntimeError("Governor trust cannot be installed over existing operations")
                db.execute("INSERT INTO b1_governor_trust VALUES(1,?,?,?)", (public_bytes, decision_json, policy_context_hash))
            elif (bytes(row["public_key"]) != public_bytes or row["decisions"] != decision_json
                  or row["policy_context_hash"] != policy_context_hash):
                raise RuntimeError("Governor trust key or policy changed")
        self._governor_public_bytes = public_bytes
        self._governor_public = Ed25519PublicKey.from_public_bytes(public_bytes)
        self.governor_registry = registry
        self._governor_decisions = decisions
        self._governor_decisions_json = decision_json
        self._governor_policy_context_hash = policy_context_hash
        self._governor_context = threading.local()

    @contextmanager
    def _shared_guard(self):
        data = getattr(self._governor_context, "request", None)
        if data is None:
            raise RuntimeError("Governor proof context missing")
        context, passage_id, epoch, _envelope = data
        with self.governor_registry.quarantine_guard():
            if context["approval"]["expires_at"] <= int(time.time()):
                raise RuntimeError("dual approval expired before authorization")
            mark = self.governor_registry._quarantine_read_locked()
            if mark.get("active") is not False:
                raise RuntimeError("shared quarantine active")
            if mark.get("lock_id", "initial") != epoch:
                raise RuntimeError("Governor receipt quarantine epoch changed")
            with self.authority._tx() as db:
                trust = db.execute("SELECT public_key, decisions, policy_context_hash FROM b1_governor_trust WHERE singleton=1").fetchone()
                if (not trust or bytes(trust["public_key"]) != self._governor_public_bytes
                        or trust["decisions"] != self._governor_decisions_json
                        or trust["policy_context_hash"] != self._governor_policy_context_hash):
                    raise RuntimeError("Governor trust key or policy changed")
            record = self.governor_registry.get(context["operation_key"])
            if not Path(self.governor_registry._store_path).is_file() or self.governor_registry.is_corrupted:
                raise RuntimeError("Governor registry unavailable or corrupt")
            command = context["command"]
            if (not record or record.state != OperationState.RESERVED
                    or record.payload_hash != canonical_payload_hash(context) or record.passage_id != passage_id
                    or record.agent_id != command["agent_id"] or record.action_type != command["action_type"]
                    or record.resource_id != command["resource_id"] or record.amount != command["amount"]):
                raise RuntimeError("Governor record no longer authorizes this request")
            yield

    def _admission_guard(self):
        return self._shared_guard()

    def _send_guard(self):
        return self._shared_guard()

    def _validate_send(self, db):
        # The SQLite writer lock is acquired after the shared quarantine gate.
        # Recheck time and trust here as acquiring that lock can take time.
        data = getattr(self._governor_context, "request", None)
        if data is None or data[0]["approval"]["expires_at"] <= int(time.time()):
            raise RuntimeError("dual approval expired before send")
        approval = db.execute("SELECT key_id, public_key FROM b1_approval_trust WHERE singleton=1").fetchone()
        governor = db.execute("SELECT public_key, decisions, policy_context_hash FROM b1_governor_trust WHERE singleton=1").fetchone()
        if (not approval or approval["key_id"] != self._approval_key_id
                or bytes(approval["public_key"]) != self._approval_public_bytes
                or not governor or bytes(governor["public_key"]) != self._governor_public_bytes
                or governor["decisions"] != self._governor_decisions_json
                or governor["policy_context_hash"] != self._governor_policy_context_hash):
            raise RuntimeError("dual authority trust changed before send")

    @contextmanager
    def checked_request(self, obj):
        """Verify the two-proof envelope and scope its shared-gate context."""
        if not isinstance(obj, dict) or "governor_receipt" not in obj:
            raise RuntimeError("Governor receipt required")
        request = snapshot_payload({key: value for key, value in obj.items() if key != "governor_receipt"})
        receipt = obj["governor_receipt"]
        fields = _RECEIPT_FIELDS
        if not isinstance(receipt, dict) or set(receipt) != fields | {"signature"}:
            raise RuntimeError("Governor receipt malformed")
        body = {name: receipt[name] for name in fields}
        if (body["schema"] != "tenir.b1.governor-receipt.v1" or body["audience"] != "positive-uid-laboratory"
                or body["decision"] not in self._governor_decisions or body["operation_key"] != request.get("operation_key")
                or body["evaluation_id"] != request.get("evaluation_id")
                or body["request_hash"] != canonical_payload_hash(request)
                or body["policy_context_hash"] != self._governor_policy_context_hash
                or not isinstance(request.get("approval"), dict)
                or body["expires_at"] != request["approval"].get("expires_at")
                or body["key_id"] != hashlib.sha256(self._governor_public_bytes).hexdigest()
                or type(body["expires_at"]) is not int or body["expires_at"] <= int(time.time())):
            raise RuntimeError("Governor receipt binding refused")
        try:
            signature = receipt["signature"]
            if not isinstance(signature, str) or len(signature) != 128:
                raise ValueError("bad signature")
            self._governor_public.verify(bytes.fromhex(signature), canonical_approval(body))
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise RuntimeError("Governor receipt signature refused") from exc
        self._governor_context.request = (
            request,
            body["passage_id"],
            body["quarantine_epoch"],
            {"request": request, "governor_receipt": snapshot_payload(receipt)},
        )
        try:
            yield request
        finally:
            del self._governor_context.request

    def _dispatch(self, obj):
        with self.checked_request(obj) as request:
            return super()._dispatch_after_governor_verification(request)
