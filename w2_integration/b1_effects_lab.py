"""Durable HTTP contract laboratory: Governor AND independent approval.

Only local mock connectors are accepted. This is not a production gateway,
secret store, global deployment fence, or a claim of real payment execution.
"""
from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import json
import os
import sqlite3
import time
import uuid

from cryptography.exceptions import InvalidSignature
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from b1_approved_positive_uid_service import canonical_approval
from b1_dual_authority import DualApprovedUidService, GovernorReceiptIssuer, _check_independent
from b1_effect_connectors import MockEffectConnector, MockStripeConnector
from native_dispatch_adapter import Ed25519ConnectorAttestor
from tenir_governance.operations.provider import ProviderOutcome, ReconciliationResult
from w2_2b_canonical import request_hash
from w2_integration.native_dispatch_command import snapshot_payload


class GrantRefused(RuntimeError):
    pass


class IntentConflict(RuntimeError):
    pass


def _digest(data):
    return hashlib.sha256(canonical_approval(data)).hexdigest()


def validate_intent(action, payload):
    payload = snapshot_payload(payload)
    if action not in ("generic_http_call", "stripe_charge") or set(payload) != {"amount", "currency", "destination"}:
        raise ValueError("unsupported effect intent")
    amount = payload["amount"]
    if type(amount) not in (int, float) or amount <= 0:
        raise ValueError("positive amount required")
    minor = Decimal(str(amount)) * 100
    if minor != minor.to_integral_value() or minor > 99999999:
        raise ValueError("amount must have at most two decimal places and fit the lab limit")
    payload["amount"] = float(minor / 100)
    if payload["currency"] not in ("EUR", "USD", "GBP"):
        raise ValueError("lab supports explicit EUR, USD and GBP only")
    if not isinstance(payload["destination"], str) or not 0 < len(payload["destination"]) <= 256:
        raise ValueError("explicit destination required")
    return {"action_type": action, "payload": payload}


class EffectContractLab:
    def __init__(self, issuer, gate, connectors, attestor, *, agent_id, resource_id):
        if os.environ.get("TENIR_ENV") not in ("test", "lab") or os.environ.get("HOST_SCOPE", "single-host") != "single-host":
            raise RuntimeError("effect contract is a single-host test/lab only")
        if type(issuer) is not GovernorReceiptIssuer or type(gate) is not DualApprovedUidService:
            raise RuntimeError("actual Governor issuer and dual proof gate required")
        if issuer._decisions != frozenset({"PASS"}) or gate._governor_decisions != frozenset({"PASS"}):
            raise RuntimeError("effect contract requires PASS-only Governor policy")
        if (issuer._receipt_authority is not gate.authority or issuer.governor.registry is not gate.governor_registry
                or issuer._public != gate._governor_public_bytes
                or issuer._independent_public != gate._approval_public_bytes
                or issuer.policy_context_hash != gate._governor_policy_context_hash):
            raise RuntimeError("issuer, gate and native store must share one configured authority")
        if (set(connectors) != {"generic_http_call", "stripe_charge"}
                or type(connectors["generic_http_call"]) is not MockEffectConnector
                or type(connectors["stripe_charge"]) is not MockStripeConnector):
            raise RuntimeError("both mock-only effect connectors required")
        if type(attestor) is not Ed25519ConnectorAttestor or gate.authority.trusted_provider_key_id != attestor.key_id:
            raise RuntimeError("native-pinned connector attestor required")
        self.issuer, self.gate, self.authority = issuer, gate, gate.authority
        self.connectors, self.attestor = dict(connectors), attestor
        self.agent_id, self.resource_id = agent_id, resource_id
        with self.authority._write("effect_contract_schema") as db:
            db.execute("CREATE TABLE IF NOT EXISTS b1_effect_intents (operation_id TEXT PRIMARY KEY, "
                       "intent_key TEXT NOT NULL UNIQUE, prepared_json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS b1_effect_grants (grant_id TEXT PRIMARY KEY, "
                       "operation_id TEXT NOT NULL UNIQUE, envelope_json TEXT NOT NULL, "
                       "signed_body_json TEXT NOT NULL, wire_json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS b1_effect_commands (operation_id TEXT PRIMARY KEY, "
                       "intent_key TEXT NOT NULL UNIQUE, approval_id TEXT NOT NULL UNIQUE, nonce TEXT NOT NULL UNIQUE, "
                       "grant_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, "
                       "observation_json TEXT, claim_json TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS b1_effect_observations (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "operation_id TEXT NOT NULL, observation_json TEXT NOT NULL)")
            for table in ("b1_effect_intents", "b1_effect_grants", "b1_effect_observations"):
                for verb in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'effect authorization is append-only'); END")

    def prepare(self, action_type, payload):
        """Trusted Governor-side preparation; not exposed as a worker HTTP route."""
        intent = validate_intent(action_type, payload)
        key = _digest(intent)
        with self.authority._write("effect_prepare") as db:
            prior = db.execute("SELECT prepared_json FROM b1_effect_intents WHERE intent_key=?", (key,)).fetchone()
            if prior:
                return json.loads(prior["prepared_json"])
            operation = str(uuid.uuid4())
            command = {"schema": "tenir-dispatch-command/v1", "resource_id": self.resource_id,
                       "agent_id": self.agent_id, "amount": intent["payload"]["amount"], "action_type": action_type,
                       "metadata": {"effect_intent": intent, "intent_hash": key}}
            self.connectors[action_type].validate({"operation_id": operation, "idempotency_key": key, **intent})
            now = int(time.time())
            body = {"schema": "tenir.b1.approval.v1", "audience": "positive-uid-laboratory",
                    "approval_id": str(uuid.uuid4()), "nonce": str(uuid.uuid4()), "operation_key": operation,
                    "evaluation_id": str(uuid.uuid4()), "idempotency_key": operation,
                    "request_hash": request_hash(command), "issued_at": now, "expires_at": now + 300,
                    "key_id": hashlib.sha256(self.issuer._independent_public).hexdigest()}
            prepared = {"schema": "tenir.positive.approved.uid-lab.v1", "action": "submit", "operation_key": operation,
                        "evaluation_id": body["evaluation_id"], "idempotency_key": operation,
                        "command": command, "approval": body}
            db.execute("INSERT INTO b1_effect_intents VALUES(?,?,?)", (operation, key, canonical_approval(prepared).decode()))
            return prepared

    def approve(self, independently_signed_request):
        """Actual adjudication followed by a durable, signed HTTP grant."""
        request = snapshot_payload(independently_signed_request)
        command, _ = _check_independent(request, self.issuer._independent_public)
        operation = request["operation_key"]
        with self.authority._tx() as db:
            prepared = db.execute("SELECT prepared_json FROM b1_effect_intents WHERE operation_id=?", (operation,)).fetchone()
            prior = db.execute("SELECT wire_json, envelope_json FROM b1_effect_grants WHERE operation_id=?", (operation,)).fetchone()
        unsigned = snapshot_payload(request)
        unsigned["approval"].pop("signature")
        if not prepared or canonical_approval(unsigned).decode() != prepared["prepared_json"]:
            raise GrantRefused("intent was not prepared by this Governor")
        if prior:
            if json.loads(prior["envelope_json"])["request"] != request:
                raise IntentConflict("approval changed")
            return json.loads(prior["wire_json"])
        receipt = self.issuer.issue(request)
        envelope = {"request": request, "governor_receipt": receipt}
        intent = command["metadata"]["effect_intent"]
        body = {"schema": "tenir.effect-http-grant/lab-v1", "grant_id": str(uuid.uuid4()),
                "audience": "effect-contract-laboratory", "operation_id": operation,
                "idempotency_key": command["metadata"]["intent_hash"], **intent,
                "dual_proof_hash": _digest(envelope)}
        signature = base64.b64encode(self.issuer._key.sign(canonical_approval(body))).decode("ascii")
        wire = {"operation_id": operation, "idempotency_key": body["idempotency_key"], **intent,
                "authority_context": {"grant_id": body["grant_id"], "signature": signature}}
        with self.gate.checked_request({**request, "governor_receipt": receipt}), self.gate._shared_guard():
            with self.authority._write("effect_http_grant") as db:
                prior = db.execute("SELECT wire_json FROM b1_effect_grants WHERE operation_id=?", (operation,)).fetchone()
                if prior:
                    return json.loads(prior["wire_json"])
                db.execute("INSERT INTO b1_effect_grants VALUES(?,?,?,?,?)", (
                    body["grant_id"], operation, canonical_approval(envelope).decode(),
                    canonical_approval(body).decode(), canonical_approval(wire).decode()))
        return wire

    def _lookup(self, wire):
        wire = snapshot_payload(wire)
        if set(wire) != {"operation_id", "idempotency_key", "action_type", "authority_context", "payload"}:
            raise GrantRefused("complete signed effect request required")
        intent = validate_intent(wire["action_type"], wire["payload"])
        wire["payload"] = intent["payload"]
        try:
            operation = uuid.UUID(wire["operation_id"])
            if operation.version != 4 or str(operation) != wire["operation_id"]:
                raise ValueError("operation UUIDv4 required")
        except (ValueError, TypeError, AttributeError) as exc:
            raise GrantRefused("operation UUIDv4 required") from exc
        if wire["idempotency_key"] != _digest(intent):
            raise IntentConflict("intent idempotency hash changed")
        context = wire["authority_context"]
        if not isinstance(context, dict) or set(context) != {"grant_id", "signature"}:
            raise GrantRefused("grant required")
        if not all(isinstance(context[name], str) for name in context):
            raise GrantRefused("grant fields invalid")
        with self.authority._tx() as db:
            row = db.execute("SELECT * FROM b1_effect_grants WHERE grant_id=?", (context["grant_id"],)).fetchone()
        if row is None:
            raise GrantRefused("grant unknown")
        try:
            signature = base64.b64decode(context["signature"], validate=True)
            self.issuer._key.public_key().verify(signature, row["signed_body_json"].encode())
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise GrantRefused("grant signature invalid") from exc
        if row["wire_json"] != canonical_approval(wire).decode():
            raise IntentConflict("grant payload or identity changed")
        envelope = json.loads(row["envelope_json"])
        if json.loads(row["signed_body_json"])["dual_proof_hash"] != _digest(envelope):
            raise GrantRefused("stored dual proof changed")
        return wire, envelope

    def submit(self, wire):
        wire, envelope = self._lookup(wire)
        operation = wire["operation_id"]
        with self.authority._tx() as db:
            prior = db.execute("SELECT operation_id FROM b1_effect_commands WHERE intent_key=?", (wire["idempotency_key"],)).fetchone()
        if prior:
            if prior["operation_id"] != operation:
                raise IntentConflict("idempotency key already belongs to another operation")
            return self.status(operation)  # No dispatch, no automatic reconciliation.
        request = envelope["request"]
        with self.gate.checked_request({**request, "governor_receipt": envelope["governor_receipt"]}), self.gate._shared_guard():
            dual_grant = self.authority.bind_b1_dual_envelope(envelope)
            def enqueue(db):
                _check_independent(request, self.issuer._independent_public)
                self.gate._validate_send(db)  # trust/time checked under the writer lock
                existing = db.execute("SELECT grant_id FROM b1_effect_commands WHERE operation_id=?", (operation,)).fetchone()
                if existing:
                    if existing["grant_id"] != wire["authority_context"]["grant_id"]:
                        raise IntentConflict("queued grant changed")
                    return
                db.execute("INSERT INTO b1_effect_commands "
                           "(operation_id,intent_key,approval_id,nonce,grant_id,status) VALUES(?,?,?,?,?,'PENDING')",
                           (operation, wire["idempotency_key"], request["approval"]["approval_id"],
                            request["approval"]["nonce"], wire["authority_context"]["grant_id"]))
            command = request["command"]
            self.authority.admit_reserve_issue(operation, request["evaluation_id"], command,
                                               resource_id=command["resource_id"],
                                               dual_grant_id=dual_grant["dual_grant_id"],
                                               _admit_hook=enqueue)
        return self.status(operation)

    def _queued(self, operation):
        with self.authority._tx() as db:
            row = db.execute("SELECT c.*, g.wire_json FROM b1_effect_commands c "
                             "JOIN b1_effect_grants g ON c.grant_id=g.grant_id WHERE c.operation_id=?", (operation,)).fetchone()
        if row is None:
            raise GrantRefused("no authorized queued operation")
        wire, envelope = self._lookup(json.loads(row["wire_json"]))
        return dict(row), wire, envelope

    def run_once(self, operation):
        row, wire, envelope = self._queued(operation)
        if row["sent"] or row["status"] != "PENDING":
            return self.status(operation)
        request, command = envelope["request"], envelope["request"]["command"]
        with self.gate.checked_request({**request, "governor_receipt": envelope["governor_receipt"]}):
            with self.gate._shared_guard():
                pass
            # Claim/witness work happens outside the quarantine gate.
            claim = self.authority.consume_and_claim(operation, command, operation)
            def mark_send(db):
                self.gate._validate_send(db)
                updated = db.execute("UPDATE b1_effect_commands SET sent=1,status='UNKNOWN',claim_json=? "
                                     "WHERE operation_id=? AND sent=0 AND status='PENDING'",
                                     (canonical_approval(claim).decode(), operation))
                if updated.rowcount != 1:
                    raise IntentConflict("effect send already elected")
                updated = db.execute("UPDATE w2_positive_v1_outbox SET status='UNKNOWN' WHERE execution_id=? AND status='PENDING'",
                                     (claim["execution_id"],))
                if updated.rowcount != 1:
                    raise IntentConflict("native outbox is not pending")
            with self.gate._shared_guard():
                decision = self.authority.grant_send_right(claim, _send_hook=mark_send)
            if decision != "GRANTED":
                return self.status(operation)
        # Durable one-send election precedes I/O. Process death cannot elect again.
        connector = self.connectors[wire["action_type"]]
        observation = connector.execute(wire)
        if observation["status"] == "CONFIRMED":
            observation = connector.reconcile(wire, observation.get("reference", ""))
        return self._record(operation, claim, command, observation)

    def reconcile_once(self, operation):
        row, wire, envelope = self._queued(operation)
        if not row["sent"] or self.status(operation)["status"] in ("CONFIRMED", "FAILED"):
            return self.status(operation)
        previous = json.loads(row["observation_json"] or "{}")
        observation = self.connectors[wire["action_type"]].reconcile(wire, previous.get("reference", ""))
        return self._record(operation, json.loads(row["claim_json"]), envelope["request"]["command"], observation)

    def _record(self, operation, claim, command, observation):
        # Serialize observation/settlement across processes without holding the
        # gate during provider I/O. An already-authorized effect may be reconciled
        # during quarantine; this grants no new send right.
        with self.gate.governor_registry.quarantine_guard():
            with self.authority._write("effect_observation_record") as db:
                db.execute("INSERT INTO b1_effect_observations (operation_id,observation_json) VALUES(?,?)",
                           (operation, canonical_approval(observation).decode()))
            current = self.status(operation)
            contradictory = {current["status"], observation["status"]} == {"FAILED", "CONFIRMED"}
            if contradictory:
                self.authority.mark_dispatch_review(claim, "contradictory provider observations")
                with self.authority._write("effect_conflict") as db:
                    db.execute("UPDATE b1_effect_commands SET status='UNKNOWN',observation_json=? WHERE operation_id=?",
                               (canonical_approval({"status": "UNKNOWN", "error": {"code": "CONTRADICTORY_EVIDENCE"}}).decode(), operation))
                return self.status(operation)
            if current["status"] in ("CONFIRMED", "FAILED") or current.get("review_required"):
                return current
            if observation["status"] == "FAILED":
                self.authority.mark_dispatch_review(claim, "proven mock rejection; budget retained for review")
            elif observation["status"] == "CONFIRMED":
                evidence = ReconciliationResult(resolved=True, outcome=ProviderOutcome.CONFIRMED,
                                                provider_reference=observation["reference"], raw_response=observation["evidence"])
                receipt = self.attestor.attest_confirmed(operation_key=operation, idempotency_key=operation, payload=command,
                                                        reconciliation=evidence, binding=self.authority.receipt_context(claim))
                self.authority.mark_dispatch_confirmed(claim, receipt)
            with self.authority._write("effect_observation") as db:
                db.execute("UPDATE b1_effect_commands SET status=?,observation_json=? "
                           "WHERE operation_id=? AND status IN ('PENDING','UNKNOWN')",
                           (observation["status"], canonical_approval(observation).decode(), operation))
        return self.status(operation)

    def status(self, operation):
        with self.authority._tx() as db:
            row = db.execute("SELECT c.status,c.observation_json,o.status AS native_status,o.provider_receipt_json "
                             "FROM b1_effect_commands c LEFT JOIN w2_positive_v1_outbox o ON o.operation_key=c.operation_id "
                             "WHERE c.operation_id=?", (operation,)).fetchone()
        if row is None:
            raise GrantRefused("operation not found")
        status = row["status"]
        result = {"operation_id": operation, "status": status, "synthetic": True, "execution_authorized": False}
        if self.authority.requires_manual_review(operation):
            result["review_required"] = True
            if row["native_status"] == "CONFIRMED":
                result["status"] = "UNKNOWN"
            observation = json.loads(row["observation_json"] or "{}")
            if observation.get("error"):
                result["error"] = observation["error"]
        elif row["native_status"] == "CONFIRMED":
            receipt = json.loads(row["provider_receipt_json"] or "{}")
            if not self.authority._verify_provider_receipt(receipt):
                raise GrantRefused("persisted receipt invalid")
            result.update(status="CONFIRMED", receipt=receipt)
        elif status == "FAILED":
            result["error"] = json.loads(row["observation_json"])["error"]
        return result


def create_effect_app(service):
    if type(service) is not EffectContractLab:
        raise RuntimeError("mock-only effect service required")
    app = FastAPI(title="TENIR effect contract laboratory", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(GrantRefused)
    async def refused(request, exc):
        return JSONResponse({"error": "grant refused"}, status_code=403)

    @app.exception_handler(IntentConflict)
    async def conflict(request, exc):
        return JSONResponse({"error": "intent conflict"}, status_code=409)

    @app.post("/api/v1/effects/execute", status_code=202)
    async def execute(request: Request):
        try:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 65536:
                    raise ValueError("request too large")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result
            wire = json.loads(data, object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
            return service.submit(wire)
        except (ValueError, TypeError, sqlite3.IntegrityError):
            return JSONResponse({"error": "invalid effect request"}, status_code=400)
        except RuntimeError as exc:
            if isinstance(exc, (GrantRefused, IntentConflict)):
                raise
            return JSONResponse({"error": "authorization unavailable"}, status_code=403)

    @app.get("/api/v1/effects/status/{operation_id}")
    def status(operation_id: str, request: Request):
        _, wire, _ = service._queued(operation_id)
        wire["authority_context"] = {"grant_id": request.headers.get("X-Tenir-Grant-Id", ""),
                                     "signature": request.headers.get("X-Tenir-Grant-Signature", "")}
        service._lookup(wire)
        return service.status(operation_id)

    return app
