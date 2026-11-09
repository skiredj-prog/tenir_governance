"""Signed approval gate for the synthetic UID laboratory, not production.

The issuer is external to this service; a signed approval is not a proof that
the issuer's policy was correct. Approval binding and native reservation share
one SQLite transaction. Grant, outbox and witness remain separate transactions.
There is no real-provider configuration here.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from positive_uid_service import ExperimentalPositiveUidService, _identifier
from w2_2b_canonical import request_hash
from w2_integration.native_dispatch_command import snapshot_command

_FIELDS = {"schema", "audience", "approval_id", "nonce", "operation_key", "evaluation_id",
           "idempotency_key", "request_hash", "issued_at", "expires_at", "key_id"}


def canonical_approval(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


class ApprovedPositiveUidService(ExperimentalPositiveUidService):
    """Retired single-approval endpoint for the synthetic UID laboratory.

    The endpoint remains importable so an installed older caller fails closed
    instead of silently selecting a different transport.  It is intentionally
    not an execution authority: an independent approval alone does not attest
    to Governor adjudication or to the current quarantine epoch.  New callers
    must use :class:`DualApprovedUidService`.
    """

    def _admission_guard(self):
        return nullcontext()

    def _send_guard(self):
        return nullcontext()

    def _validate_send(self, db):
        """Additional trusted service checks inside the send transaction."""

    def __init__(self, state_dir, socket_dir, *, worker_uids, approval_public_key):
        if not isinstance(approval_public_key, bytes) or len(approval_public_key) != 32:
            raise RuntimeError("approval public key must be 32 bytes")
        super().__init__(state_dir, socket_dir, worker_uids=worker_uids)
        try:
            self._initialize_approval_trust(approval_public_key)
        except BaseException:
            self.provider.conn.close()
            raise

    def _initialize_approval_trust(self, public_key):
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise RuntimeError("approval public key must be 32 bytes")
        key_id = hashlib.sha256(public_key).hexdigest()
        with self.authority._write("b1_approval_trust") as db:
            db.execute("CREATE TABLE IF NOT EXISTS b1_approval_trust "
                       "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), key_id TEXT NOT NULL, public_key BLOB NOT NULL)")
            row = db.execute("SELECT key_id, public_key FROM b1_approval_trust WHERE singleton=1").fetchone()
            if row is None:
                if (db.execute("SELECT 1 FROM w2_positive_v1_permits LIMIT 1").fetchone()
                        or db.execute("SELECT 1 FROM w2_positive_v1_outbox LIMIT 1").fetchone()):
                    raise RuntimeError("approval trust cannot be installed over existing positive operations")
                db.execute("INSERT INTO b1_approval_trust VALUES(1,?,?)", (key_id, public_key))
            elif row["key_id"] != key_id or bytes(row["public_key"]) != public_key:
                raise RuntimeError("approval trust key changed")
            db.execute("CREATE TABLE IF NOT EXISTS b1_approved_requests "
                       "(approval_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, "
                       "operation_key TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL UNIQUE, "
                       "body_hash TEXT NOT NULL, body_json TEXT NOT NULL, signature TEXT NOT NULL)")
            db.execute("CREATE TRIGGER IF NOT EXISTS b1_approvals_no_update BEFORE UPDATE ON b1_approved_requests "
                       "BEGIN SELECT RAISE(ABORT, 'approval binding is append-only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS b1_approvals_no_delete BEFORE DELETE ON b1_approved_requests "
                       "BEGIN SELECT RAISE(ABORT, 'approval binding is append-only'); END")
        self._approval_public = Ed25519PublicKey.from_public_bytes(public_key)
        self._approval_public_bytes = public_key
        self._approval_key_id = key_id

    def _dispatch(self, obj):
        raise RuntimeError(
            "legacy signed service disabled: Governor adjudication and independent approval required"
        )

    def _dispatch_after_governor_verification(self, obj):
        """Perform the former admission/send sequence after the dual service
        has verified a Governor receipt and installed its guarded context.

        This private method is not a protocol endpoint.  Keeping the logic in
        one place prevents the dual path from drifting from the native atomic
        approval/I8 implementation while the public legacy endpoint stays
        fail-closed.
        """
        context = getattr(getattr(self, "_governor_context", None), "request", None)
        if context is None:
            raise RuntimeError("Governor verification context required")
        dual_envelope = context[3]
        if (not isinstance(obj, dict) or obj.get("schema") != "tenir.positive.approved.uid-lab.v1"
                or obj.get("action") != "submit" or set(obj) != {
                    "schema", "action", "operation_key", "evaluation_id", "idempotency_key", "command", "approval"
                }):
            raise ValueError("signed approval request required")
        operation = _identifier(obj["operation_key"])
        evaluation = _identifier(obj["evaluation_id"])
        idempotency = _identifier(obj["idempotency_key"])
        command = snapshot_command(obj["command"])
        approval = obj["approval"]
        if not isinstance(approval, dict) or set(approval) != _FIELDS | {"signature"}:
            raise ValueError("malformed signed approval")
        body = {name: approval[name] for name in _FIELDS}
        if (body["schema"] != "tenir.b1.approval.v1" or body["audience"] != "positive-uid-laboratory"
                or body["key_id"] != self._approval_key_id or body["operation_key"] != operation
                or body["evaluation_id"] != evaluation or body["idempotency_key"] != idempotency
                or body["request_hash"] != request_hash(command)):
            raise ValueError("approval binding mismatch")
        approval_id, nonce = _identifier(body["approval_id"]), _identifier(body["nonce"])
        issued, expires = body["issued_at"], body["expires_at"]
        if type(issued) is not int or type(expires) is not int or expires <= issued or expires - issued > 86400:
            raise ValueError("approval timestamps invalid")
        signature = approval["signature"]
        if not isinstance(signature, str) or len(signature) != 128:
            raise ValueError("approval signature invalid")
        encoded = canonical_approval(body)
        try:
            self._approval_public.verify(bytes.fromhex(signature), encoded)
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("approval signature invalid") from exc
        dual_grant = self.authority.bind_b1_dual_envelope(dual_envelope)
        with self._admission_guard():
            grant = self.gate.evaluate_and_grant(
                operation, evaluation, command, resource_id=command["resource_id"],
                dual_grant_id=dual_grant["dual_grant_id"],
            )
        with self.authority._tx() as db:
            prior = db.execute("SELECT status FROM w2_positive_v1_outbox WHERE operation_key=?", (operation,)).fetchone()
        if prior and prior["status"] != "PENDING":
            return {"status": prior["status"], "synthetic": True, "execution_authorized": False}
        result = self.gate.dispatch_to_b3(operation, idempotency, command, grant, self.provider,
                                          _send_guard=self._send_guard, _send_hook=self._validate_send)
        return {"status": result["status"], "synthetic": True, "execution_authorized": False}
