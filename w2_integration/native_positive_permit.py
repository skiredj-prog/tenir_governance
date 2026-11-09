"""W2.2-AC-04 — positive permit on the native G3.1/G4-A SQLite store.

Uses consensus.sqlite3. Does not rename PositiveG4A into native authority.
Does not change ExperimentalG4AAuthority: HOLD and execution_authorized=false stay.
B3 remains outside the SQLite transaction. The independent Witness uses a
separate database; its checkpoint is verified before the native claim commits.
A failed native commit can therefore leave an unused Witness checkpoint, but
cannot create a dispatchable outbox claim.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import sqlite3
import secrets
import threading
import time
import uuid
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from tenir_governance.operations.payload_binding import canonical_payload_hash
from b1_native_dual_grant import (
    APPROVAL_FIELDS,
    DUAL_GRANT_SCHEMA,
    GOVERNOR_RECEIPT_FIELDS,
    canonical_b1,
    digest_b1,
)
from b1_native_quarantine import commit_guard, QuarantineGateBusy
from independent_witness import IndependentWitness
from w2_2b_canonical import request_hash as canon_request_hash
from w2_integration.native_dispatch_command import COMMAND_SCHEMA, snapshot_command, snapshot_payload

PERMIT_SCHEMA = "w2-positive-permit/v1"
TOPOLOGY_ID = "lab-s1-native-sqlite-single-host-2026-10-28"


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _execute_schema_statements(db, script: str) -> None:
    """Execute static DDL without executescript's implicit transaction commit."""
    if not db.in_transaction:
        raise RuntimeError("schema changes require a transaction")
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete schema statement")


class NativePositivePermitAuthority:
    def __init__(self, backend: SQLiteConsensusBackend, *, witness=None,
                 trusted_provider_public_key: bytes | None = None,
                 verifiable_recovery: bool = False):
        if not isinstance(backend, SQLiteConsensusBackend):
            raise TypeError("SQLiteConsensusBackend required")
        self.backend = backend
        self.db_path = backend._database_path
        self.topology_id = TOPOLOGY_ID
        self.productive = False
        self._accepted_grant_id = None
        # This is an in-process laboratory send fence, not an OS security boundary.
        self._positive_send_lock = threading.Lock()
        self._positive_live_sends = {}
        self._provider_live_sends = {}
        self.verifiable_recovery = verifiable_recovery
        self.external_anchor = None
        # JW-10: optional cold-restart anti-rollback gate attached to the
        # native admission and send paths. None preserves prior behaviour.
        self._cold_restart_gate = None
        self._cold_restart_subject = None
        self._cold_restart_min_generation = 1
        # JW-10: optional external monotone publisher. When attached, native
        # commits that change the durable state re-anchor it (extend-only), so
        # a gated admit->claim->send runs end-to-end with no manual republish.
        self._cold_restart_publisher = None
        self._last_anchor_publication = None
        self._b1_quarantine_frames = threading.local()
        # JW-10: check any existing kernel custody pin BEFORE schema writes,
        # key generation or accepting a caller-provided witness object.
        with self._tx() as custody_db:
            exists=custody_db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='w2_verifiable_custody'"
            ).fetchone()
            if exists:
                prior=custody_db.execute(
                    "SELECT kernel_key_id,witness_key_id FROM w2_verifiable_custody "
                    "WHERE singleton=1"
                ).fetchone()
                if prior is None:
                    raise RuntimeError("JW-10 kernel custody pin missing: HOLD")
                if not verifiable_recovery:
                    raise RuntimeError("JW-10 kernel verifiable custody downgrade prohibited")
            elif verifiable_recovery:
                # No retroactive upgrading of preexisting native authority.
                prior_data=custody_db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('w2_positive_v1_permits','w2_positive_v1_outbox')"
                ).fetchall()
                for item in prior_data:
                    if custody_db.execute(
                        'SELECT 1 FROM "'+item["name"]+'" LIMIT 1'
                    ).fetchone():
                        raise RuntimeError("JW-10 cannot upgrade legacy kernel claims")
        self.witness = (
            witness if witness is not None
            else IndependentWitness(self.db_path + ".witness.sqlite3", verifiable_recovery=verifiable_recovery)
        )
        if trusted_provider_public_key is None:
            self.trusted_provider_public_key = None
            self.trusted_provider_key_id = None
        else:
            if not isinstance(trusted_provider_public_key, bytes) or len(trusted_provider_public_key) != 32:
                raise ValueError("trusted provider public key must be 32 raw Ed25519 bytes")
            self.trusted_provider_public_key = Ed25519PublicKey.from_public_bytes(
                trusted_provider_public_key
            )
            self.trusted_provider_key_id = hashlib.sha256(
                trusted_provider_public_key
            ).hexdigest()
        # Creation, migrations and trigger replacement form one writer
        # transaction. executescript() would implicitly commit and expose a
        # missing-trigger window to other processes.
        with self.backend._transaction("positive_schema") as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='w2_positive_v1_keys'"
            ).fetchone():
                raise RuntimeError("refusing store that contains w2_positive_v1_keys")
            _execute_schema_statements(db, """
                CREATE TABLE IF NOT EXISTS w2_positive_v1_capacity (
                  operation_key TEXT PRIMARY KEY,
                  amount INTEGER NOT NULL,
                  reserved INTEGER NOT NULL DEFAULT 1,
                  settled INTEGER NOT NULL DEFAULT 0,
                  reservation_id TEXT,
                  resource_id TEXT
                );
                CREATE TABLE IF NOT EXISTS w2_positive_v1_permits (
                  permit_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE,
                  evaluation_id TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  reservation_id TEXT NOT NULL,
                  issued_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  consumed INTEGER NOT NULL DEFAULT 0,
                  dispatch_claim INTEGER NOT NULL DEFAULT 0,
                  idempotency_key TEXT,
                  payload_json TEXT,
                  signature BLOB NOT NULL,
                  key_id TEXT NOT NULL,
                  schema_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS w2_positive_v1_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  operation_key TEXT NOT NULL,
                  event TEXT NOT NULL,
                  at TEXT NOT NULL,
                  detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS w2_positive_v1_outbox (
                  execution_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  request_hash TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  claim_token TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL CHECK(status IN ('PENDING','UNKNOWN','CONFIRMED','FAILED')),
                  created_at TEXT NOT NULL,
                  witness_evidence_json TEXT,
                  witness_hash TEXT,
                  provider_receipt_json TEXT
                );
                CREATE TABLE IF NOT EXISTS b1_native_dual_grants (
                  dual_grant_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  evaluation_id TEXT NOT NULL,
                  command_hash TEXT NOT NULL,
                  envelope_hash TEXT NOT NULL UNIQUE,
                  approval_id TEXT NOT NULL UNIQUE,
                  approval_nonce TEXT NOT NULL UNIQUE,
                  approval_hash TEXT NOT NULL,
                  approval_body_json TEXT NOT NULL,
                  approval_signature TEXT NOT NULL,
                  approval_key_id TEXT NOT NULL,
                  governor_receipt_hash TEXT NOT NULL,
                  governor_receipt_json TEXT NOT NULL,
                  governor_key_id TEXT NOT NULL,
                  audience TEXT NOT NULL,
                  expires_at INTEGER NOT NULL,
                  quarantine_epoch TEXT NOT NULL,
                  policy_context_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  schema_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS b1_native_dual_permit_links (
                  dual_grant_id TEXT PRIMARY KEY,
                  operation_key TEXT NOT NULL UNIQUE,
                  permit_id TEXT NOT NULL UNIQUE,
                  reservation_id TEXT NOT NULL UNIQUE,
                  command_hash TEXT NOT NULL
                );
            """)
            self._add_column(db, "w2_positive_v1_capacity", "reservation_id", "TEXT")
            self._add_column(db, "w2_positive_v1_capacity", "resource_id", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "nonce_hash", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "nonce_status", "TEXT NOT NULL DEFAULT 'ISSUED'")
            self._add_column(db, "w2_positive_v1_permits", "execution_id", "TEXT")
            self._add_column(db, "w2_positive_v1_permits", "audience", "TEXT NOT NULL DEFAULT 'kernel-dispatch'")
            self._add_column(db, "w2_positive_v1_outbox", "witness_evidence_json", "TEXT")
            self._add_column(db, "w2_positive_v1_outbox", "witness_hash", "TEXT")
            self._add_column(db, "w2_positive_v1_outbox", "provider_receipt_json", "TEXT")
            _execute_schema_statements(db, """
                DROP TRIGGER IF EXISTS w2_block_historical_after_positive;
                CREATE TRIGGER w2_block_historical_after_positive
                BEFORE INSERT ON reservations
                WHEN EXISTS (
                  SELECT 1 FROM w2_positive_v1_capacity
                  WHERE operation_key = NEW.idempotency_key
                    AND (reservation_id IS NULL OR reservation_id != NEW.reservation_id)
                )
                BEGIN
                  SELECT RAISE(ABORT, 'positive capacity owns this key');
                END;
                DROP TRIGGER IF EXISTS w2_block_positive_after_historical;
                CREATE TRIGGER w2_block_positive_after_historical
                BEFORE INSERT ON w2_positive_v1_capacity
                WHEN EXISTS (
                  SELECT 1 FROM reservations
                  WHERE idempotency_key = NEW.operation_key
                )
                BEGIN
                  SELECT RAISE(ABORT, 'historical reservation owns this key');
                END;
                CREATE TRIGGER IF NOT EXISTS w2_positive_v1_events_no_update
                BEFORE UPDATE ON w2_positive_v1_events
                BEGIN
                  SELECT RAISE(ABORT, 'positive authority events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS w2_positive_v1_events_no_delete
                BEFORE DELETE ON w2_positive_v1_events
                BEGIN
                  SELECT RAISE(ABORT, 'positive authority events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS b1_native_dual_grants_no_update
                BEFORE UPDATE ON b1_native_dual_grants
                BEGIN
                  SELECT RAISE(ABORT, 'B1 native dual grant is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS b1_native_dual_grants_no_delete
                BEFORE DELETE ON b1_native_dual_grants
                BEGIN
                  SELECT RAISE(ABORT, 'B1 native dual grant is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS b1_native_dual_permit_links_no_update
                BEFORE UPDATE ON b1_native_dual_permit_links
                BEGIN
                  SELECT RAISE(ABORT, 'B1 native dual permit link is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS b1_native_dual_permit_links_no_delete
                BEFORE DELETE ON b1_native_dual_permit_links
                BEGIN
                  SELECT RAISE(ABORT, 'B1 native dual permit link is append-only');
                END;
            """)
        self._load_key()
        if verifiable_recovery:
            if self.key_custody != "EXTERNAL_HEX" or self.witness.key_custody != "EXTERNAL_HEX":
                raise RuntimeError("JW-10 injected kernel and witness custody required")
            with self._write("jw10_custody_pin") as db:
                db.execute("CREATE TABLE IF NOT EXISTS w2_verifiable_custody "
                           "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                           "kernel_key_id TEXT NOT NULL, witness_key_id TEXT NOT NULL)")
                row=db.execute("SELECT kernel_key_id,witness_key_id "
                               "FROM w2_verifiable_custody WHERE singleton=1").fetchone()
                expected=(self.key_id,self.witness.key_id)
                if row is not None and tuple(row)!=expected:
                    raise RuntimeError("JW-10 kernel/witness key changed on restart")
                if row is None:
                    db.execute("INSERT INTO w2_verifiable_custody VALUES(1,?,?)",expected)

    def local_witness_revision(self, subject: str) -> int:
        with sqlite3.connect(self.witness.db_path) as db:
            row = db.execute(
                "SELECT record_json FROM witness_checkpoints WHERE subject=?",
                (subject,),
            ).fetchone()
        if not row:
            return 0
        return int(json.loads(row[0])["revision"])

    def check_anchor(self, anchor, subject: str) -> None:
        anchor.require_not_behind(subject, self.local_witness_revision(subject))

    def attach_cold_restart_anchor_gate(self, gate, *, subject: str,
                                        min_generation: int = 1,
                                        publisher=None) -> None:
        """Attach an independent cold-restart anti-rollback gate to native paths.

        Once attached, ``admit_reserve_issue``, ``consume_and_claim`` and
        ``grant_send_right`` refuse to proceed unless the committed
        Kernel+Witness state matches the separately trusted CURRENT anchor
        read by ``gate``. This converts the
        standalone ``ExternalColdRestartAnchorGate`` verifier into an enforced
        precondition for native operations.

        **Bounded, not closed.** The on-disk fingerprint is computed on the
        committed, quiescent state *before* the native write transaction opens;
        it is NOT an atomic cross-database snapshot taken with the write. The
        gate therefore proves the committed state matched the external current
        anchor at check time, not that the operation about to be written is
        anchored. A stale/compromised reader that replays a validly-signed old
        checkpoint is still accepted (signature alone is not freshness). Full
        enforcement requires a genuinely external, live monotone authority.
        """
        if not callable(getattr(gate, "verify_cold_restart", None)):
            raise TypeError("cold-restart anchor gate must expose verify_cold_restart")
        if not isinstance(subject, str) or not subject:
            raise ValueError("cold-restart anchor subject required")
        if type(min_generation) is not int or min_generation < 1:
            raise ValueError("cold-restart min_generation must be a positive int")
        self._cold_restart_gate = gate
        self._cold_restart_subject = subject
        self._cold_restart_min_generation = min_generation
        self._cold_restart_publisher = publisher

    def detach_cold_restart_anchor_gate(self) -> None:
        self._cold_restart_gate = None
        self._cold_restart_subject = None
        self._cold_restart_min_generation = 1
        self._cold_restart_publisher = None

    def _enforce_cold_restart_anchor(self, operation: str):
        """Fail closed on native paths when the attached anchor gate HOLDs.

        No-op when no gate is attached, so unconfigured laboratories keep their
        prior semantics. The ``ExternalAnchorHold`` raised by the gate (missing,
        forged, stale, mismatching or rolled-back checkpoint, or reader outage)
        is re-raised tagged with the refused native ``operation``.
        """
        gate = self._cold_restart_gate
        if gate is None:
            return None
        try:
            return gate.verify_cold_restart(
                subject=self._cold_restart_subject,
                kernel_db=self.db_path,
                witness_db=self.witness.db_path,
                min_generation=self._cold_restart_min_generation,
            )
        except Exception as exc:
            # Keep the gate's original message (tests match on it) and name the
            # native operation that is being refused.
            raise type(exc)(f"{operation}: {exc}") from exc

    def _publish_cold_restart_anchor_after_commit(self, operation: str):
        """Re-anchor the committed state after a native write (extend-only).

        No-op without an attached publisher. The publication is store-derived
        and idempotent: an unchanged state is a no-op, a Kernel-only change in
        the same Witness epoch advances the Kernel digest, and a new Witness
        checkpoint extends the chain. A publication HOLD (anti-downgrade, fork,
        equivocation) is a genuine anomaly and is allowed to propagate.
        """
        publisher = self._cold_restart_publisher
        if publisher is None:
            return None
        from w2_integration.cold_restart_anchor_publisher import publish_cold_restart_anchor
        self._last_anchor_publication = publish_cold_restart_anchor(
            self, publisher, subject=self._cold_restart_subject,
        )
        return self._last_anchor_publication

    def _tx(self):
        """Autocommit connection for schema and reads. Not a write transaction."""
        return self.backend._connection()

    @staticmethod
    def _add_column(db, table, column, definition):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def _write(self, operation: str):
        """BEGIN IMMEDIATE with mandatory JW-10 custody enforcement.

        Also fences objects opened BEFORE a different object pins this store.
        Caller discipline and opt-in constructor checks alone are inadequate.
        """
        # Final quarantine lease outlives the transaction context, including
        # COMMIT/fault injection, but is never held during witness/provider I/O.
        with ExitStack() as leases, self.backend._transaction(operation) as db:
            frames = getattr(self._b1_quarantine_frames, "frames", None)
            if frames is None:
                frames = self._b1_quarantine_frames.frames = {}
            checks = frames[id(db)] = []
            try:
                self._check_write_custody(db)
                yield db
                if checks:
                    leases.enter_context(commit_guard(db, checks))
            finally:
                del frames[id(db)]

    def _check_write_custody(self, db):
        """Custody validation shared by every native write transaction."""
        has_pin=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='w2_verifiable_custody'"
        ).fetchone()
        if has_pin:
            pinned=db.execute(
                "SELECT kernel_key_id,witness_key_id "
                "FROM w2_verifiable_custody WHERE singleton=1"
            ).fetchone()
            if pinned is None:
                raise RuntimeError("JW-10 kernel custody pin missing: HOLD")
            if not self.verifiable_recovery:
                raise RuntimeError("JW-10 previous Kernel instance fenced by custody pin")
            if (pinned["kernel_key_id"]!=self.key_id
                    or pinned["witness_key_id"]!=self.witness.key_id):
                raise RuntimeError("JW-10 kernel/witness key changed on restart")

    def _require_b1_live_quarantine(self, db, row, payload):
        checks = getattr(self._b1_quarantine_frames, "frames", {}).get(id(db))
        if checks is None:
            # Read-only claim verification grants no new send right. A valid
            # one-use capability elected before quarantine remains in flight.
            return
        check = (dict(row), snapshot_payload(payload))
        with commit_guard(db, [check]):
            pass
        checks.append(check)  # re-read under the gate held through COMMIT

    def _require_native_i8(self, db, operation_key: str, payload: dict, *, allow_committed: bool = False) -> None:
        """Refuse a claim or settlement when the durable I8 row no longer matches."""
        cap = db.execute(
            "SELECT amount, reserved, settled, reservation_id, resource_id "
            "FROM w2_positive_v1_capacity WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        if not cap or not cap["reservation_id"] or not cap["resource_id"]:
            raise RuntimeError("native I8 reservation missing")
        reservation = db.execute(
            "SELECT * FROM reservations WHERE reservation_id=?",
            (cap["reservation_id"],),
        ).fetchone()
        if not reservation:
            raise RuntimeError("native I8 reservation missing")
        if reservation["idempotency_key"] != operation_key:
            raise RuntimeError("native I8 reservation key mismatch")
        if reservation["resource_id"] != cap["resource_id"]:
            raise RuntimeError("native I8 resource mismatch")
        cap_amount = self.backend._amount(cap["amount"])
        res_amount = self.backend._amount(reservation["amount"])
        payload_amount = self.backend._amount(payload.get("amount")) if isinstance(payload, dict) else None
        if cap_amount is None or res_amount is None or cap_amount != res_amount:
            raise RuntimeError("native I8 amount mismatch")
        if payload_amount is not None and payload_amount != res_amount:
            raise RuntimeError("native I8 amount mismatch")
        if isinstance(payload, dict) and payload.get("resource_id") not in (None, reservation["resource_id"]):
            raise RuntimeError("native I8 resource mismatch")
        allowed = ("RESERVED", "COMMITTED") if allow_committed else ("RESERVED",)
        if reservation["state"] not in allowed:
            raise RuntimeError("native I8 reservation state invalid")
        resource = db.execute(
            "SELECT reserved, committed FROM resources WHERE resource_id=?",
            (reservation["resource_id"],),
        ).fetchone()
        if not resource:
            raise RuntimeError("native I8 budget missing")
        if reservation["state"] == "RESERVED":
            reserved = self.backend._amount(resource["reserved"])
            if reserved is None or reserved < res_amount or not cap["reserved"] or cap["settled"]:
                raise RuntimeError("native I8 budget invariant broken")
        else:
            committed = self.backend._amount(resource["committed"])
            if committed is None or committed < res_amount or cap["reserved"] or not cap["settled"]:
                raise RuntimeError("native I8 budget invariant broken")

    def _load_key(self):
        raw_hex = os.environ.get("TENIR_W2_PRIVATE_KEY_HEX")
        if raw_hex:
            try:
                raw = bytes.fromhex(raw_hex.strip())
            except ValueError as exc:
                raise RuntimeError("TENIR_W2_PRIVATE_KEY_HEX is not hex") from exc
            if len(raw) != 32:
                raise RuntimeError("TENIR_W2_PRIVATE_KEY_HEX must be 32 bytes")
            self.private_key = Ed25519PrivateKey.from_private_bytes(raw)
            self.key_custody = "EXTERNAL_HEX"
        else:
            self.private_key = Ed25519PrivateKey.generate()
            self.key_custody = "LAB_EPHEMERAL_MEMORY"
        self.productive = False
        pub = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(pub).hexdigest()

    def _body(self, permit_id, op, evaluation_id, request_hash, reservation_id, issued_at, expires_at,
              nonce_hash=None, audience="kernel-dispatch") -> dict:
        return {
            "schema": PERMIT_SCHEMA,
            "permit_id": permit_id,
            "operation_key": op,
            "evaluation_id": evaluation_id,
            "request_hash": request_hash,
            "reservation_id": reservation_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "nonce_hash": nonce_hash,
            "audience": audience,
            "key_id": self.key_id,
            "decision": "ALLOW",
            "scope": "experimental-positive-v1",
        }

    @staticmethod
    def _b1_profile_enabled(db) -> bool:
        """Return whether this store has opted into the B1 dual-proof profile.

        A generic W2 laboratory has no B1 trust material and keeps its existing
        experimental semantics.  Once *any* B1 trust table is present, a
        partial profile is an error rather than a route back to native-only
        admission.
        """
        required = {
            "b1_approval_trust",
            "b1_governor_trust",
            "b1_governor_issuer_trust",
            "b1_governor_receipts",
            "b1_approved_requests",
        }
        names = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        configured = names & required
        if not configured:
            return False
        if configured != required:
            raise RuntimeError("B1 dual trust profile is incomplete")
        return True

    @staticmethod
    def _require_text(value, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
            raise RuntimeError(f"B1 {name} required")
        return value

    def _b1_trust(self, db) -> dict:
        """Load and cross-check the durable B1 trust pins under the writer lock."""
        if not self._b1_profile_enabled(db):
            raise RuntimeError("B1 dual trust profile required")
        approval = db.execute(
            "SELECT key_id, public_key FROM b1_approval_trust WHERE singleton=1"
        ).fetchone()
        governor = db.execute(
            "SELECT public_key, decisions, policy_context_hash FROM b1_governor_trust "
            "WHERE singleton=1"
        ).fetchone()
        issuer = db.execute(
            "SELECT public_key FROM b1_governor_issuer_trust WHERE singleton=1"
        ).fetchone()
        if not approval or not governor or not issuer:
            raise RuntimeError("B1 dual trust pin missing")
        approval_key = bytes(approval["public_key"])
        governor_key = bytes(governor["public_key"])
        issuer_key = bytes(issuer["public_key"])
        if (len(approval_key) != 32 or len(governor_key) != 32
                or issuer_key != governor_key or approval_key == governor_key):
            raise RuntimeError("B1 dual trust pin integrity failure")
        if approval["key_id"] != hashlib.sha256(approval_key).hexdigest():
            raise RuntimeError("approval trust key changed: B1 independent key identifier mismatch")
        try:
            decisions = frozenset(json.loads(governor["decisions"]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("B1 Governor trust key or policy changed") from exc
        if not decisions or not decisions <= {"PASS", "FLAG"}:
            raise RuntimeError("B1 Governor trust key or policy changed")
        context_hash = governor["policy_context_hash"]
        if (not isinstance(context_hash, str) or len(context_hash) != 64
                or any(char not in "0123456789abcdef" for char in context_hash)):
            raise RuntimeError("B1 Governor policy context invalid")
        return {
            "approval_key": approval_key,
            "approval_key_id": approval["key_id"],
            "governor_key": governor_key,
            "governor_key_id": hashlib.sha256(governor_key).hexdigest(),
            "decisions": decisions,
            "policy_context_hash": context_hash,
        }

    def _validate_b1_dual_envelope(self, db, envelope: dict) -> dict:
        """Validate both signed proofs against SQLite-pinned B1 trust.

        The caller supplies bytes to verify, not a verdict.  The resulting
        record is later rechecked by admission, claim and send transactions.
        """
        trust = self._b1_trust(db)
        if not isinstance(envelope, dict) or set(envelope) != {"request", "governor_receipt"}:
            raise RuntimeError("complete B1 dual proof envelope required")
        request = snapshot_payload(envelope["request"])
        expected_request_fields = {
            "schema", "action", "operation_key", "evaluation_id", "idempotency_key", "command", "approval",
        }
        if (set(request) != expected_request_fields
                or request["schema"] != "tenir.positive.approved.uid-lab.v1"
                or request["action"] != "submit"):
            raise RuntimeError("B1 signed request malformed")
        operation = self._require_text(request["operation_key"], "operation")
        evaluation = self._require_text(request["evaluation_id"], "evaluation")
        idempotency = self._require_text(request["idempotency_key"], "idempotency")
        if operation != idempotency:
            raise RuntimeError("B1 approval idempotency must equal operation")
        command = snapshot_command(request["command"])
        command_hash = canon_request_hash(command)
        approval = request["approval"]
        if not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS | {"signature"}:
            raise RuntimeError("B1 independent approval malformed")
        approval_body = {name: approval[name] for name in APPROVAL_FIELDS}
        for name in ("approval_id", "nonce", "operation_key", "evaluation_id", "idempotency_key"):
            self._require_text(approval_body[name], "approval " + name)
        now = int(time.time())
        if (approval_body["schema"] != "tenir.b1.approval.v1"
                or approval_body["audience"] != "positive-uid-laboratory"
                or approval_body["key_id"] != trust["approval_key_id"]
                or approval_body["operation_key"] != operation
                or approval_body["evaluation_id"] != evaluation
                or approval_body["idempotency_key"] != idempotency
                or approval_body["request_hash"] != command_hash
                or type(approval_body["issued_at"]) is not int
                or type(approval_body["expires_at"]) is not int
                or approval_body["issued_at"] > now + 30
                or approval_body["expires_at"] <= now
                or approval_body["expires_at"] <= approval_body["issued_at"]
                or approval_body["expires_at"] - approval_body["issued_at"] > 86400):
            raise RuntimeError("B1 independent approval binding or validity refused")
        try:
            approval_signature = approval["signature"]
            if not isinstance(approval_signature, str) or len(approval_signature) != 128:
                raise ValueError("invalid signature")
            Ed25519PublicKey.from_public_bytes(trust["approval_key"]).verify(
                bytes.fromhex(approval_signature), canonical_b1(approval_body)
            )
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise RuntimeError("B1 independent approval signature refused") from exc

        receipt = envelope["governor_receipt"]
        if not isinstance(receipt, dict) or set(receipt) != GOVERNOR_RECEIPT_FIELDS | {"signature"}:
            raise RuntimeError("B1 Governor receipt malformed")
        receipt_body = {name: receipt[name] for name in GOVERNOR_RECEIPT_FIELDS}
        request_hash = canonical_payload_hash(request)
        if (receipt_body["schema"] != "tenir.b1.governor-receipt.v1"
                or receipt_body["audience"] != "positive-uid-laboratory"
                or receipt_body["operation_key"] != operation
                or receipt_body["evaluation_id"] != evaluation
                or receipt_body["request_hash"] != request_hash
                or receipt_body["decision"] not in trust["decisions"]
                or receipt_body["expires_at"] != approval_body["expires_at"]
                or receipt_body["policy_context_hash"] != trust["policy_context_hash"]
                or receipt_body["key_id"] != trust["governor_key_id"]
                or not isinstance(receipt_body["quarantine_epoch"], str)
                or not receipt_body["quarantine_epoch"]):
            raise RuntimeError("B1 Governor receipt binding refused")
        try:
            receipt_signature = receipt["signature"]
            if not isinstance(receipt_signature, str) or len(receipt_signature) != 128:
                raise ValueError("invalid signature")
            Ed25519PublicKey.from_public_bytes(trust["governor_key"]).verify(
                bytes.fromhex(receipt_signature), canonical_b1(receipt_body)
            )
        except (InvalidSignature, TypeError, ValueError) as exc:
            raise RuntimeError("B1 Governor receipt signature refused") from exc
        persisted_receipt = db.execute(
            "SELECT request_hash, receipt_json FROM b1_governor_receipts WHERE operation_key=?",
            (operation,),
        ).fetchone()
        if not persisted_receipt:
            raise RuntimeError("B1 Governor receipt not durably recorded")
        try:
            stored_receipt = json.loads(persisted_receipt["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("B1 stored Governor receipt invalid") from exc
        if (persisted_receipt["request_hash"] != request_hash
                or stored_receipt != receipt):
            raise RuntimeError("B1 stored Governor receipt diverges")

        envelope_hash = digest_b1({"request": request, "governor_receipt": receipt})
        return {
            "dual_grant_id": "dual-" + envelope_hash,
            "operation_key": operation,
            "idempotency_key": idempotency,
            "evaluation_id": evaluation,
            "command_hash": command_hash,
            "envelope_hash": envelope_hash,
            "approval_id": approval_body["approval_id"],
            "approval_nonce": approval_body["nonce"],
            "approval_hash": digest_b1(approval_body),
            "approval_body_json": canonical_b1(approval_body).decode("utf-8"),
            "approval_signature": approval_signature,
            "approval_key_id": trust["approval_key_id"],
            "governor_receipt_hash": digest_b1(receipt_body),
            "governor_receipt_json": canonical_b1(receipt).decode("utf-8"),
            "governor_key_id": trust["governor_key_id"],
            "audience": receipt_body["audience"],
            "expires_at": approval_body["expires_at"],
            "quarantine_epoch": receipt_body["quarantine_epoch"],
            "policy_context_hash": trust["policy_context_hash"],
            "schema_id": DUAL_GRANT_SCHEMA,
        }

    def bind_b1_dual_envelope(self, envelope: dict) -> dict:
        """Durably bind verified Governor and independent proofs for B1.

        It is safe to replay the *identical* envelope.  A substituted proof,
        key, command or identity conflicts before an I8 reservation exists.

        JW-10: a new bind mutates durable Kernel state, so it appends an event
        to the append-only ``w2_positive_v1_events`` log **in the same
        transaction** and re-anchors after commit. Without that event the
        bind would change the Kernel digest while minting no ordering mark,
        which the external monotone anchor cannot distinguish from a rollback.
        """
        # Gate the write on the cold-restart anchor like the other native paths.
        self._enforce_cold_restart_anchor("b1_native_dual_bind")
        inserted = False
        with self._write("b1_native_dual_bind") as db:
            proof = self._validate_b1_dual_envelope(db, envelope)
            matches = db.execute(
                "SELECT * FROM b1_native_dual_grants WHERE operation_key=? OR idempotency_key=? "
                "OR approval_id=? OR approval_nonce=?",
                (proof["operation_key"], proof["idempotency_key"], proof["approval_id"], proof["approval_nonce"]),
            ).fetchall()
            if matches:
                if len(matches) != 1:
                    raise RuntimeError("approval binding conflict: B1 dual proof identity mismatch")
                existing = dict(matches[0])
                if any(existing[name] != proof[name] for name in proof):
                    raise RuntimeError("approval binding conflict: B1 dual proof identity mismatch")
            else:
                db.execute(
                    """INSERT INTO b1_native_dual_grants
                       (dual_grant_id, operation_key, idempotency_key, evaluation_id,
                        command_hash, envelope_hash, approval_id, approval_nonce,
                        approval_hash, approval_body_json, approval_signature,
                        approval_key_id, governor_receipt_hash, governor_receipt_json,
                        governor_key_id, audience, expires_at, quarantine_epoch,
                        policy_context_hash, created_at, schema_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        proof["dual_grant_id"], proof["operation_key"], proof["idempotency_key"],
                        proof["evaluation_id"], proof["command_hash"], proof["envelope_hash"],
                        proof["approval_id"], proof["approval_nonce"], proof["approval_hash"],
                        proof["approval_body_json"], proof["approval_signature"],
                        proof["approval_key_id"], proof["governor_receipt_hash"],
                        proof["governor_receipt_json"], proof["governor_key_id"],
                        proof["audience"], proof["expires_at"], proof["quarantine_epoch"],
                        proof["policy_context_hash"], datetime.now(timezone.utc).isoformat(),
                        proof["schema_id"],
                    ),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                    "VALUES (?, 'b1_dual_grant_bound', ?, ?)",
                    (proof["operation_key"], datetime.now(timezone.utc).isoformat(),
                     proof["dual_grant_id"]),
                )
                inserted = True
        if inserted:
            self._publish_cold_restart_anchor_after_commit("b1_native_dual_bind")
        return proof

    def _require_b1_current_trust(self, db, row):
        trust = self._b1_trust(db)
        try:
            decision = json.loads(row["governor_receipt_json"])["decision"]
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError("B1 stored Governor decision invalid") from exc
        if (row["approval_key_id"] != trust["approval_key_id"]
                or row["governor_key_id"] != trust["governor_key_id"]
                or row["policy_context_hash"] != trust["policy_context_hash"]
                or decision not in trust["decisions"]):
            raise RuntimeError("B1 dual grant trust changed")

    def _require_b1_dual_admission(self, db, dual_grant_id, operation_key, evaluation_id, payload):
        """Require the durable proof before any B1 I8 write or extension hook."""
        if not self._b1_profile_enabled(db):
            return None
        if not isinstance(dual_grant_id, str) or not dual_grant_id:
            raise RuntimeError("durable B1 dual grant required before native admission")
        row = db.execute(
            "SELECT * FROM b1_native_dual_grants WHERE dual_grant_id=?", (dual_grant_id,)
        ).fetchone()
        command_hash = canon_request_hash(payload)
        if (not row or row["schema_id"] != DUAL_GRANT_SCHEMA
                or row["operation_key"] != operation_key
                or row["idempotency_key"] != operation_key
                or row["evaluation_id"] != evaluation_id
                or row["command_hash"] != command_hash
                or row["expires_at"] <= int(time.time())):
            raise RuntimeError("durable B1 dual grant does not authorize native admission")
        self._require_b1_current_trust(db, row)
        self._require_b1_live_quarantine(db, row, payload)
        existing = db.execute(
            "SELECT * FROM b1_approved_requests WHERE approval_id=? OR nonce=? "
            "OR operation_key=? OR idempotency_key=?",
            (row["approval_id"], row["approval_nonce"], operation_key, operation_key),
        ).fetchall()
        if existing:
            if (len(existing) != 1 or existing[0]["approval_id"] != row["approval_id"]
                    or existing[0]["nonce"] != row["approval_nonce"]
                    or existing[0]["operation_key"] != operation_key
                    or existing[0]["idempotency_key"] != operation_key
                    or existing[0]["body_hash"] != row["approval_hash"]
                    or existing[0]["body_json"] != row["approval_body_json"]
                    or existing[0]["signature"] != row["approval_signature"]):
                raise RuntimeError("B1 durable approval binding conflict")
            if not db.execute(
                "SELECT 1 FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,),
            ).fetchone():
                raise RuntimeError("orphaned approval binding requires review")
        else:
            db.execute(
                "INSERT INTO b1_approved_requests VALUES(?,?,?,?,?,?,?)",
                (
                    row["approval_id"], row["approval_nonce"], operation_key, operation_key,
                    row["approval_hash"], row["approval_body_json"], row["approval_signature"],
                ),
            )
        return row

    def _link_b1_dual_permit(self, db, dual, permit_id, reservation_id):
        if dual is None:
            return
        row = db.execute(
            "SELECT * FROM b1_native_dual_permit_links WHERE dual_grant_id=? OR operation_key=?",
            (dual["dual_grant_id"], dual["operation_key"]),
        ).fetchone()
        expected = {
            "dual_grant_id": dual["dual_grant_id"],
            "operation_key": dual["operation_key"],
            "permit_id": permit_id,
            "reservation_id": reservation_id,
            "command_hash": dual["command_hash"],
        }
        if row:
            if any(row[name] != value for name, value in expected.items()):
                raise RuntimeError("B1 dual permit link conflict")
            return
        db.execute(
            "INSERT INTO b1_native_dual_permit_links VALUES(?,?,?,?,?)",
            tuple(expected[name] for name in (
                "dual_grant_id", "operation_key", "permit_id", "reservation_id", "command_hash",
            )),
        )

    def _require_b1_dual_binding(self, db, operation_key, idempotency_key, payload, *, require_live: bool):
        """Revalidate the durable B1 link before claim verification or send."""
        if not self._b1_profile_enabled(db):
            return None
        row = db.execute(
            """SELECT g.*, l.permit_id AS linked_permit_id, l.reservation_id AS linked_reservation_id
               FROM b1_native_dual_grants g
               JOIN b1_native_dual_permit_links l ON l.dual_grant_id=g.dual_grant_id
               WHERE g.operation_key=? AND g.idempotency_key=?""",
            (operation_key, idempotency_key),
        ).fetchone()
        if (not row or row["schema_id"] != DUAL_GRANT_SCHEMA
                or row["command_hash"] != canon_request_hash(payload)):
            raise RuntimeError("durable B1 dual grant missing or mismatched")
        if require_live and row["expires_at"] <= int(time.time()):
            raise RuntimeError("durable B1 dual grant expired before send")
        permit = db.execute(
            "SELECT permit_id, reservation_id FROM w2_positive_v1_permits WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        if (not permit or permit["permit_id"] != row["linked_permit_id"]
                or permit["reservation_id"] != row["linked_reservation_id"]):
            raise RuntimeError("durable B1 dual grant permit link invalid")
        self._require_b1_current_trust(db, row)
        if require_live:
            self._require_b1_live_quarantine(db, row, payload)
        return row

    def admit_reserve_issue(
        self, operation_key: str, evaluation_id: str, payload: dict,
        amount=None, *, resource_id: str, dual_grant_id: str | None = None, _admit_hook=None,
    ) -> dict:
        """Atomically reserve native I8 capacity and issue its bound positive permit.

        The amount defaults to the canonical payload's ``amount`` field. The I8
        resource must already exist in SQLiteConsensusBackend. The private hook
        composes trusted writer-side checks with the same SQLite transaction;
        it is not a security boundary against code running as the writer.
        """
        # JW-10: enforce the cold-restart anti-rollback gate on admission, in a
        # quiescent read before the writer transaction opens. HOLD propagates.
        self._enforce_cold_restart_anchor("positive_admit")
        payload = snapshot_payload(payload)
        if payload.get("schema") == COMMAND_SCHEMA:
            payload = snapshot_command(payload)
        if amount is None:
            if not isinstance(payload, dict) or "amount" not in payload:
                raise RuntimeError("native I8 amount must be explicit or present in payload")
            amount = payload["amount"]
        normalized_amount = self.backend._amount(amount)
        if normalized_amount is None:
            raise RuntimeError("invalid native I8 amount")
        if (isinstance(payload, dict) and "amount" in payload
                and self.backend._amount(payload["amount"]) != normalized_amount):
            raise RuntimeError("native I8 amount does not match payload amount")
        if not isinstance(resource_id, str) or not resource_id:
            raise RuntimeError("native I8 resource_id required")
        if (isinstance(payload, dict) and payload.get("resource_id") is not None
                and payload.get("resource_id") != resource_id):
            raise RuntimeError("payload resource_id does not match native I8 resource")
        request_hash = canon_request_hash(payload)
        issued_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        nonce_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        permit_id = f"permit-{operation_key}"
        reservation_id = f"res-{operation_key}"
        body = self._body(permit_id, operation_key, evaluation_id, request_hash, reservation_id, issued_at,
                          expires_at, nonce_hash)
        signature = self.private_key.sign(_canonical(body))
        try:
            with self._write("positive_admit") as db:
                dual = self._require_b1_dual_admission(
                    db, dual_grant_id, operation_key, evaluation_id, payload,
                )
                if _admit_hook is not None:
                    _admit_hook(db)
                existing = db.execute(
                    "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if existing:
                    capacity_row = db.execute(
                        "SELECT amount, reservation_id, resource_id FROM w2_positive_v1_capacity "
                        "WHERE operation_key=?", (operation_key,),
                    ).fetchone()
                    reservation = db.execute(
                        "SELECT reservation_id, resource_id, amount, state FROM reservations "
                        "WHERE reservation_id=? AND idempotency_key=?",
                        (existing["reservation_id"], operation_key),
                    ).fetchone()
                    if (existing["request_hash"] != request_hash
                            or existing["evaluation_id"] != evaluation_id
                            or not capacity_row or not reservation
                            or capacity_row["reservation_id"] != reservation["reservation_id"]
                            or capacity_row["resource_id"] != resource_id
                            or self.backend._amount(capacity_row["amount"]) != normalized_amount
                            or reservation["resource_id"] != resource_id
                            or self.backend._amount(reservation["amount"]) != normalized_amount
                            or reservation["state"] not in ("RESERVED", "COMMITTED")):
                        raise RuntimeError("contradictory or incomplete native I8 permit")
                    # A profile-enabled store must not retrofit a freshly
                    # supplied proof onto a permit that predates the dual
                    # boundary.  That would turn a legacy reservation into a
                    # dispatchable B1 operation after the fact.  New permits
                    # receive their link below in this very transaction;
                    # replayed permits must already have the exact link.
                    if dual is not None:
                        linked = db.execute(
                            "SELECT dual_grant_id, operation_key, permit_id, reservation_id, command_hash "
                            "FROM b1_native_dual_permit_links WHERE permit_id=?",
                            (existing["permit_id"],),
                        ).fetchone()
                        if not linked:
                            raise RuntimeError("unapproved existing permit: unlinked B1 permit requires review")
                    self._link_b1_dual_permit(
                        db, dual, existing["permit_id"], existing["reservation_id"],
                    )
                    return dict(existing)
                historical = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('reservations','experimental_native_operations')"
                ).fetchall()
                names = {row["name"] for row in historical}
                if "reservations" in names:
                    prior = db.execute(
                        "SELECT reservation_id FROM reservations WHERE idempotency_key=?",
                        (operation_key,),
                    ).fetchone()
                    if prior:
                        raise RuntimeError("historical I8 reservation exists; positive authority refused")
                if "experimental_native_operations" in names:
                    prior = db.execute(
                        "SELECT operation_key FROM experimental_native_operations WHERE operation_key=?",
                        (operation_key,),
                    ).fetchone()
                    if prior:
                        raise RuntimeError("historical native operation exists; positive authority refused")
                db.execute(
                    "INSERT INTO w2_positive_v1_capacity "
                    "(operation_key, amount, reservation_id, resource_id) VALUES (?, ?, ?, ?)",
                    (operation_key, str(normalized_amount), reservation_id, resource_id),
                )
                if os.environ.get("W2_FAIL_AFTER_CAPACITY") == "1":
                    raise RuntimeError("injected failure after capacity")
                reservation_result = self.backend._reserve_in_connection(
                    db, operation_key, normalized_amount, resource_id,
                    reservation_id=reservation_id, positive_authority_owner=True,
                )
                if reservation_result.status != "RESERVED":
                    raise RuntimeError(
                        f"native I8 reservation refused: {reservation_result.status}"
                    )
                db.execute(
                    """INSERT INTO w2_positive_v1_permits
                       (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                       issued_at, expires_at, nonce_hash, execution_id, audience, signature, key_id, schema_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (permit_id, operation_key, evaluation_id, request_hash, reservation_id,
                     issued_at, expires_at, nonce_hash, permit_id, "kernel-dispatch",
                     signature, self.key_id, PERMIT_SCHEMA),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                    (operation_key, "permit_issued_after_reservation", issued_at, reservation_id),
                )
                self._link_b1_dual_permit(db, dual, permit_id, reservation_id)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("exclusive authority refused") from exc
        # JW-10: re-anchor the committed admission (Kernel-only progress within
        # the current Witness epoch) so the next gated step does not HOLD.
        self._publish_cold_restart_anchor_after_commit("positive_admit")
        return {"permit_id": permit_id, "operation_key": operation_key, "request_hash": request_hash,
                "reservation_id": reservation_id, "resource_id": resource_id,
                "amount": str(normalized_amount), "schema": PERMIT_SCHEMA,
                "execution_authorized_historical": False}

    def consume_and_claim(self, operation_key: str, payload: dict, idempotency_key: str) -> dict:
        # Retry only a rolled-back local claim on gate contention. Each attempt
        # revalidates the same identity/epoch. The witness replays its durable
        # identity; no provider call or ambiguous external effect is retried.
        deadline = time.monotonic() + 0.5
        while True:
            try:
                # A restored permit must not mint a replacement claim before
                # the live CURRENT anchor has checked the committed stores.
                # Recheck after gate contention before retrying the claim.
                self._enforce_cold_restart_anchor("positive_claim")
                result = self._consume_and_claim_once(operation_key, payload, idempotency_key)
                break
            except QuarantineGateBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        # JW-10: a new claim minted a fresh Witness checkpoint (revision += 1).
        # A real process kill here, after the local Kernel+Witness commits but
        # before the external anchor is extended, leaves the next gated op in
        # HOLD until extend-only recovery republishes.
        if (result.get("claimed") and not result.get("replayed")
                and os.environ.get("W2_CRASH_BEFORE_ANCHOR_PUBLICATION") == "1"):
            os._exit(1)
        if result.get("claimed") and not result.get("replayed"):
            self._publish_cold_restart_anchor_after_commit("positive_claim")
        return result

    def _consume_and_claim_once(self, operation_key: str, payload: dict, idempotency_key: str) -> dict:
        request_hash = canon_request_hash(payload)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        witness_subject = f"{operation_key}:{idempotency_key}"
        try:
            with self._write("positive_claim") as db:
                now = datetime.now(timezone.utc).isoformat()
                replay = db.execute(
                    "SELECT execution_id, operation_key, idempotency_key, request_hash, claim_token, status, "
                    "witness_evidence_json, witness_hash, provider_receipt_json "
                    "FROM w2_positive_v1_outbox WHERE operation_key=? OR idempotency_key=?",
                    (operation_key, idempotency_key),
                ).fetchone()
                self._require_b1_dual_binding(
                    db, operation_key, idempotency_key, payload,
                    require_live=not replay or replay["status"] == "PENDING",
                )
                review_latched = db.execute(
                    "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                    "AND event='dispatch_review' LIMIT 1", (operation_key,),
                ).fetchone()
                if replay:
                    if replay["operation_key"] != operation_key or replay["idempotency_key"] != idempotency_key or replay["request_hash"] != request_hash:
                        raise RuntimeError("idempotency key conflict")
                    self._require_native_i8(
                        db, operation_key, payload, allow_committed=replay["status"] == "CONFIRMED",
                    )
                    evidence_json = replay["witness_evidence_json"]
                    witness_hash = replay["witness_hash"]
                    if not evidence_json or not witness_hash:
                        raise RuntimeError("claim witness evidence missing")
                    evidence = json.loads(evidence_json)
                    if evidence.get("record", {}).get("subject") != witness_subject:
                        raise RuntimeError("witness subject mismatch")
                    expected_witness_hash = hashlib.sha256(
                        b"TENIR-W2-CLAIM-WITNESS-v1\0" + _canonical(evidence)
                    ).hexdigest()
                    if (expected_witness_hash != witness_hash
                            or not self.witness.verify(evidence, subject=witness_subject)):
                        raise RuntimeError("claim witness evidence invalid")
                    return {"operation_key": operation_key, "idempotency_key": idempotency_key,
                            "execution_id": replay["execution_id"], "claim_token": replay["claim_token"],
                            "token": replay["claim_token"], "nonce": replay["claim_token"],
                            "operation": operation_key, "audience": "kernel-dispatch",
                            "status": replay["status"], "claimed": True, "replayed": True,
                            "witness": evidence, "witness_hash": witness_hash,
                            "receipt": json.loads(replay["provider_receipt_json"])
                            if replay["provider_receipt_json"] else None}
                if review_latched:
                    raise RuntimeError("manual review precedes send")
                grant_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='w2_positive_g4a_v1_grants'"
                ).fetchone()
                if grant_table:
                    grant_row = db.execute(
                        "SELECT grant_id FROM w2_positive_g4a_v1_grants WHERE operation_key=?",
                        (operation_key,),
                    ).fetchone()
                    if grant_row and self._accepted_grant_id != grant_row["grant_id"]:
                        raise RuntimeError("positive G4-A grant required before claim")
                row = db.execute(
                    "SELECT * FROM w2_positive_v1_permits WHERE operation_key=?", (operation_key,)
                ).fetchone()
                if not row or row["request_hash"] != request_hash:
                    raise RuntimeError("no valid unconsumed permit")
                if row["consumed"] or row["nonce_status"] != "ISSUED":
                    raise RuntimeError("nonce already consumed")
                if row["audience"] != "kernel-dispatch":
                    raise RuntimeError("claim audience mismatch")
                if row["expires_at"] <= now:
                    raise RuntimeError("permit expired")
                if row["key_id"] != self.key_id:
                    raise RuntimeError("key_id mismatch")
                body = self._body(row["permit_id"], row["operation_key"], row["evaluation_id"],
                                  row["request_hash"], row["reservation_id"], row["issued_at"], row["expires_at"],
                                  row["nonce_hash"], row["audience"])
                try:
                    self.private_key.public_key().verify(row["signature"], _canonical(body))
                except (InvalidSignature, ValueError) as exc:
                    raise RuntimeError("permit signature invalid") from exc
                cap = db.execute(
                    "SELECT reserved, settled FROM w2_positive_v1_capacity WHERE operation_key=?",
                    (operation_key,),
                ).fetchone()
                if not cap or not cap["reserved"]:
                    raise RuntimeError("no native reservation")
                self._require_native_i8(db, operation_key, payload, allow_committed=False)
                witness_fingerprint = hashlib.sha256(
                    b"TENIR-W2-CLAIM-FINGERPRINT-v1\0" + _canonical({
                        "operation_key": operation_key,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "permit_id": row["permit_id"],
                        "reservation_id": row["reservation_id"],
                        "audience": row["audience"],
                    })
                ).hexdigest()
                try:
                    proposed_execution = f"exec-{uuid.uuid4().hex}"
                    proposed_token = secrets.token_urlsafe(32)
                    witness_evidence = self.witness.issue(
                        witness_subject, witness_fingerprint,
                        identity={"execution_id": proposed_execution, "claim_token": proposed_token},
                    )
                    if not self.witness.verify(witness_evidence, subject=witness_subject):
                        return {
                            "operation_key": operation_key, "idempotency_key": idempotency_key,
                            "operation": operation_key, "audience": "kernel-dispatch",
                            "execution_id": None, "claim_token": None, "token": None, "nonce": None,
                            "status": "REFUSED", "claimed": False, "witness": witness_evidence,
                            "witness_hash": None,
                        }
                except Exception as exc:
                    raise RuntimeError("independent witness refused claim") from exc
                record = witness_evidence["record"]
                if (record.get("subject") != witness_subject
                        or record.get("snapshot_fingerprint") != witness_fingerprint
                        or not isinstance(record.get("execution_id"), str)
                        or not record["execution_id"]
                        or not isinstance(record.get("claim_token"), str)
                        or not record["claim_token"]):
                    raise RuntimeError("JW-09 foreign or incomplete witness resolution")
                execution_id = record["execution_id"]
                claim_token = record["claim_token"]
                witness_evidence_json = json.dumps(
                    witness_evidence, sort_keys=True, separators=(",", ":")
                )
                witness_hash = hashlib.sha256(
                    b"TENIR-W2-CLAIM-WITNESS-v1\0" + _canonical(witness_evidence)
                ).hexdigest()
                updated = db.execute(
                    """UPDATE w2_positive_v1_permits
                       SET consumed=1, dispatch_claim=1, idempotency_key=?, payload_json=?,
                           nonce_status='CONSUMED', execution_id=?
                       WHERE operation_key=? AND consumed=0 AND nonce_status='ISSUED' AND request_hash=?""",
                    (idempotency_key, payload_json, execution_id, operation_key, request_hash),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("atomic claim failed")
                db.execute(
                    "INSERT INTO w2_positive_v1_outbox "
                    "(execution_id, operation_key, idempotency_key, request_hash, payload_json, claim_token, status, created_at, witness_evidence_json, witness_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
                    (execution_id, operation_key, idempotency_key, request_hash, payload_json, claim_token,
                     now, witness_evidence_json, witness_hash),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) VALUES (?, ?, ?, ?)",
                    (operation_key, "dispatch_claimed", now, idempotency_key),
                )
                if os.environ.get("W2_CRASH_BEFORE_NATIVE_CLAIM_COMMIT") == "1":
                    os.kill(os.getpid(), __import__("signal").SIGKILL)
                if self.external_anchor is not None:
                    self.external_anchor.note(
                        witness_subject, record["revision"], execution_id, claim_token,
                    )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("exclusive authority refused") from exc
        if os.environ.get("W2_CRASH_AFTER_NATIVE_CLAIM") == "1":
            os.kill(os.getpid(), __import__("signal").SIGKILL)
        return {"operation_key": operation_key, "idempotency_key": idempotency_key,
                "operation": operation_key, "audience": "kernel-dispatch",
                "execution_id": execution_id, "claim_token": claim_token,
                "token": claim_token, "nonce": claim_token,
                "status": "PENDING", "claimed": True,
                "witness": witness_evidence, "witness_hash": witness_hash}

    def latch_review_before_send(self, operation_key: str, reason: str) -> str:
        """Freeze an operation before any send right is granted.

        A review that arrives after the right was granted is still recorded,
        but it cannot erase that grant. The freeze is written before attempting
        extend-only anchor publication, so an unavailable anchor cannot undo it.
        """
        outcome = self._latch_review_before_send_locked(operation_key, reason)
        self._publish_cold_restart_anchor_after_commit("latch_review_before_send")
        return outcome

    def _latch_review_before_send_locked(self, operation_key: str, reason: str) -> str:
        if not isinstance(operation_key, str) or not operation_key:
            raise RuntimeError("operation required")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError("manual review reason required")
        now = datetime.now(timezone.utc).isoformat()
        with self._write("latch_review_before_send") as db:
            granted = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_send_granted' LIMIT 1", (operation_key,),
            ).fetchone()
            prior = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (operation_key,),
            ).fetchone()
            if not prior:
                detail = json.dumps(
                    {"reason": reason.strip(), "before_send": granted is None},
                    sort_keys=True, separators=(",", ":"),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                    "VALUES (?, 'dispatch_review', ?, ?)",
                    (operation_key, now, detail),
                )
            return "REVIEW_AFTER_GRANT" if granted else "HOLD"

    def grant_send_right(self, claim: dict, *, _send_hook=None) -> str:
        """Gate (JW-10), elect a single sender, then re-anchor on grant.

        The anti-rollback gate runs before the writer transaction; the election
        decision is the append-only ``dispatch_send_granted`` event; and a
        ``GRANTED`` outcome re-anchors the committed state (extend-only) so a
        subsequent gated step does not HOLD.
        """
        # JW-10: enforce the cold-restart anti-rollback gate on the send path,
        # in a quiescent read before the writer transaction opens.
        self._enforce_cold_restart_anchor("grant_send_right")
        outcome = self._grant_send_right_locked(claim, _send_hook=_send_hook)
        if outcome == "GRANTED":
            self._publish_cold_restart_anchor_after_commit("grant_send_right")
        return outcome

    def _grant_send_right_locked(self, claim: dict, *, _send_hook=None) -> str:
        """Give exactly one caller the right to send this claim.

        The decision is the append-only ``dispatch_send_granted`` event, taken
        in the same transaction that observes a manual-review latch. Replay and
        crash do not insert a second event and therefore must not send again.
        """
        if not isinstance(claim, dict):
            raise RuntimeError("dispatch claim required")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        now = datetime.now(timezone.utc).isoformat()
        with self._write("grant_send_right") as db:
            row = db.execute(
                "SELECT operation_key, idempotency_key, payload_json, status, claim_token FROM w2_positive_v1_outbox "
                "WHERE execution_id=?", (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            try:
                command = json.loads(row["payload_json"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("native outbox payload invalid") from exc
            self._require_b1_dual_binding(
                db, row["operation_key"], row["idempotency_key"], command, require_live=True,
            )
            if db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (row["operation_key"],),
            ).fetchone():
                return "REVIEW"
            granted = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_send_granted' LIMIT 1", (row["operation_key"],),
            ).fetchone()
            if granted or row["status"] != "PENDING":
                return "ALREADY"
            permit = db.execute(
                "SELECT expires_at FROM w2_positive_v1_permits WHERE operation_key=?",
                (row["operation_key"],),
            ).fetchone()
            if not permit or permit["expires_at"] <= datetime.now(timezone.utc).isoformat():
                return "EXPIRED"
            if _send_hook is not None:
                _send_hook(db)
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                "VALUES (?, 'dispatch_send_granted', ?, ?)",
                (row["operation_key"], now, execution_id),
            )
            return "GRANTED"

    def _grant_kernel_provider_send(self, claim: dict):
        """Issue an in-process, one-use lab-provider capability after election.

        A crash after election cannot recreate the capability from SQLite.
        This is not protection against code running as the trusted writer UID.
        """
        decision = self.grant_send_right(claim)
        if decision != "GRANTED":
            return decision, None
        capability = secrets.token_urlsafe(32)
        with self._positive_send_lock:
            self._provider_live_sends[claim["execution_id"]] = (claim["claim_token"], capability)
        return decision, capability

    def _consume_kernel_provider_send(self, claim: dict) -> bool:
        if not isinstance(claim, dict):
            return False
        execution = claim.get("execution_id")
        token = claim.get("claim_token")
        capability = claim.get("provider_send_capability")
        if not all(isinstance(x, str) and x for x in (execution, token, capability)):
            return False
        with self._positive_send_lock:
            expected = self._provider_live_sends.get(execution)
            if (not expected or not secrets.compare_digest(expected[0], token)
                    or not secrets.compare_digest(expected[1], capability)):
                return False
            del self._provider_live_sends[execution]
            return True

    def _discard_kernel_provider_send(self, claim: dict) -> None:
        with self._positive_send_lock:
            self._provider_live_sends.pop(claim.get("execution_id"), None)

    def load_dispatch_payload(self, claim: dict) -> dict:
        """Read the committed outbox command for this exact native capability.

        This authenticates the payload, not a right to send repeatedly. Dispatch
        election, review interleaving and public settlement fencing are separate
        gates and are not supplied by this read.
        """
        if not isinstance(claim, dict):
            raise RuntimeError("native dispatch claim required")
        with self._tx() as db:
            row = db.execute(
                "SELECT o.payload_json, o.request_hash, o.operation_key, o.idempotency_key, "
                "p.request_hash AS permit_hash FROM w2_positive_v1_outbox o "
                "JOIN w2_positive_v1_permits p ON p.operation_key=o.operation_key "
                "AND p.execution_id=o.execution_id "
                "WHERE o.execution_id=? AND o.claim_token=?",
                (claim.get("execution_id"), claim.get("claim_token")),
            ).fetchone()
            if (not row or claim.get("operation_key") != row["operation_key"]
                    or claim.get("idempotency_key") != row["idempotency_key"]):
                raise RuntimeError("native outbox claim identity mismatch")
            try:
                payload = json.loads(row["payload_json"])
                actual_hash = canon_request_hash(payload)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("persisted command payload invalid") from exc
            if actual_hash != row["request_hash"] or actual_hash != row["permit_hash"]:
                raise RuntimeError("persisted command payload hash mismatch")
            self._require_native_i8(db, row["operation_key"], payload, allow_committed=True)
            return payload

    def mark_dispatch_unknown(self, claim: dict) -> str:
        """Retain an ambiguous dispatch, then extend the anchor after commit.

        The local UNKNOWN must be durable even if publication cannot complete.
        A failed publication leaves later positive paths in HOLD until the
        existing extend-only recovery protocol republishes the committed state.
        """
        outcome = self._mark_dispatch_unknown_locked(claim)
        if outcome == "UNKNOWN":
            self._publish_cold_restart_anchor_after_commit("mark_dispatch_unknown")
        return outcome

    def _mark_dispatch_unknown_locked(self, claim: dict) -> str:
        if not isinstance(claim, dict):
            raise RuntimeError("dispatch claim required")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        with self._write("mark_dispatch_unknown") as db:
            row = db.execute(
                "SELECT status, claim_token FROM w2_positive_v1_outbox WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if row["status"] == "CONFIRMED":
                return "CONFIRMED"
            if row["status"] == "UNKNOWN":
                return "UNKNOWN"
            if row["status"] != "PENDING":
                raise RuntimeError("dispatch is not eligible for UNKNOWN")
            updated = db.execute(
                "UPDATE w2_positive_v1_outbox SET status='UNKNOWN' "
                "WHERE execution_id=? AND claim_token=? AND status='PENDING'",
                (execution_id, token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("dispatch status race")
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                "VALUES (?, 'dispatch_unknown', ?, ?)",
                (claim["operation_key"], datetime.now(timezone.utc).isoformat(), execution_id),
            )
            return "UNKNOWN"

    def mark_dispatch_review(self, claim: dict, reason: str) -> str:
        """Durably freeze an ambiguous/contradictory provider result for humans."""
        outcome = self._mark_dispatch_review_locked(claim, reason)
        self._publish_cold_restart_anchor_after_commit("mark_dispatch_review")
        return outcome

    def _mark_dispatch_review_locked(self, claim: dict, reason: str) -> str:
        if not isinstance(claim, dict) or not claim.get("execution_id"):
            raise RuntimeError("dispatch claim identity missing")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError("manual review reason required")
        execution_id = claim["execution_id"]
        token = claim.get("claim_token")
        with self._write("mark_dispatch_review") as db:
            row = db.execute(
                "SELECT operation_key, status, claim_token FROM w2_positive_v1_outbox "
                "WHERE execution_id=?", (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if row["status"] not in ("PENDING", "UNKNOWN", "CONFIRMED"):
                raise RuntimeError("dispatch cannot enter manual review")
            if row["status"] == "PENDING":
                updated = db.execute(
                    "UPDATE w2_positive_v1_outbox SET status='UNKNOWN' "
                    "WHERE execution_id=? AND claim_token=? AND status='PENDING'",
                    (execution_id, token),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("dispatch review status race")
            prior = db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (row["operation_key"],),
            ).fetchone()
            if not prior:
                detail = json.dumps(
                    {"execution_id": execution_id, "reason": reason.strip()},
                    sort_keys=True, separators=(",", ":"),
                )
                db.execute(
                    "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                    "VALUES (?, 'dispatch_review', ?, ?)",
                    (row["operation_key"], datetime.now(timezone.utc).isoformat(), detail),
                )
            return "CONFIRMED_REVIEW" if row["status"] == "CONFIRMED" else "UNKNOWN"

    def requires_manual_review(self, operation_key: str) -> bool:
        with self._tx() as db:
            return db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (operation_key,),
            ).fetchone() is not None

    def _verify_provider_receipt(self, receipt: dict) -> bool:
        if not isinstance(receipt, dict) or self.trusted_provider_public_key is None:
            return False
        try:
            if receipt.get("key_id") != self.trusted_provider_key_id:
                return False
            signature = base64.b64decode(receipt["provider_signature"], validate=True)
            unsigned = {key: value for key, value in receipt.items()
                        if key != "provider_signature"}
            self.trusted_provider_public_key.verify(signature, _canonical(unsigned))
            return True
        except Exception:
            return False

    def mark_dispatch_confirmed(self, claim: dict, receipt: dict) -> str:
        """Gate the settlement on the cold-restart anchor, then confirm.

        Settlement commits the I8 reservation, so the anti-rollback gate extends
        to it: a rolled-back Kernel/Witness is refused before the effect is
        settled, and a successful settlement re-anchors (extend-only). The
        review/latch/unknown reconciliation paths are deliberately NOT gated --
        they must be able to freeze a dubious dispatch even on a stale anchor --
        so they rely on the Witness/outbox/receipt guards instead.
        """
        self._enforce_cold_restart_anchor("mark_dispatch_confirmed")
        outcome = self._mark_dispatch_confirmed_locked(claim, receipt)
        if outcome == "CONFIRMED":
            self._publish_cold_restart_anchor_after_commit("mark_dispatch_confirmed")
        return outcome

    def _mark_dispatch_confirmed_locked(self, claim: dict, receipt: dict) -> str:
        """Persist only a receipt matching the exact pending/unknown native claim."""
        if not isinstance(claim, dict) or not isinstance(receipt, dict):
            raise RuntimeError("claim and provider receipt required")
        if not self._verify_provider_receipt(receipt):
            raise RuntimeError("provider receipt signature invalid")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with self._write("mark_dispatch_confirmed") as db:
            row = db.execute(
                "SELECT execution_id, operation_key, idempotency_key, request_hash, claim_token, status, "
                "provider_receipt_json, witness_evidence_json, witness_hash "
                "FROM w2_positive_v1_outbox WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row or row["claim_token"] != token:
                raise RuntimeError("dispatch claim does not own outbox row")
            if (claim.get("operation") != row["operation_key"]
                    or claim.get("operation_key") != row["operation_key"]
                    or claim.get("idempotency_key") != row["idempotency_key"]
                    or claim.get("audience") != "kernel-dispatch"):
                raise RuntimeError("dispatch claim scope mismatch")
            if (receipt.get("operation_id") != row["operation_key"]
                    or receipt.get("idempotency_key") != row["idempotency_key"]
                    or receipt.get("request_hash") != row["request_hash"]
                    or receipt.get("effect_status") != "EXECUTED"):
                raise RuntimeError("provider receipt does not match claim")
            evidence_json = row["witness_evidence_json"]
            evidence = json.loads(evidence_json) if evidence_json else None
            permit = db.execute(
                "SELECT permit_id, reservation_id FROM w2_positive_v1_permits WHERE operation_key=?",
                (row["operation_key"],),
            ).fetchone()
            witness_epoch = evidence.get("record", {}).get("revision") if isinstance(evidence, dict) else None
            if (not permit
                    or receipt.get("execution_id") != row["execution_id"]
                    or receipt.get("reservation_id") != permit["reservation_id"]
                    or receipt.get("permit_id") != permit["permit_id"]
                    or receipt.get("witness_hash") != row["witness_hash"]
                    or receipt.get("witness_epoch") != witness_epoch):
                raise RuntimeError("provider receipt binding mismatch")
            if (not evidence or not row["witness_hash"]
                    or claim.get("witness_hash") != row["witness_hash"]
                    or claim.get("witness") != evidence
                    or evidence.get("record", {}).get("subject") != f"{row['operation_key']}:{row['idempotency_key']}"
                    or not self.witness.verify(
                        evidence, subject=f"{row['operation_key']}:{row['idempotency_key']}",
                    )):
                raise RuntimeError("claim witness is invalid")
            if db.execute(
                "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                "AND event='dispatch_review' LIMIT 1", (row["operation_key"],),
            ).fetchone():
                raise RuntimeError("manual review latch blocks automatic confirmation")
            if row["status"] == "CONFIRMED":
                if row["provider_receipt_json"] != receipt_json:
                    raise RuntimeError("conflicting confirmed receipt")
                reservation = db.execute(
                    "SELECT p.reservation_id, r.state FROM w2_positive_v1_permits p "
                    "JOIN reservations r ON r.reservation_id=p.reservation_id "
                    "WHERE p.operation_key=?", (row["operation_key"],),
                ).fetchone()
                cap = db.execute(
                    "SELECT reserved, settled FROM w2_positive_v1_capacity WHERE operation_key=?",
                    (row["operation_key"],),
                ).fetchone()
                if not reservation or reservation["state"] != "COMMITTED" or not cap or cap["settled"] != 1:
                    raise RuntimeError("confirmed outbox lacks committed native I8 reservation")
                return "CONFIRMED"
            if row["status"] not in ("PENDING", "UNKNOWN"):
                raise RuntimeError("dispatch is not eligible for confirmation")
            stored_payload = json.loads(db.execute(
                "SELECT payload_json FROM w2_positive_v1_outbox WHERE execution_id=?",
                (execution_id,),
            ).fetchone()["payload_json"])
            self._require_native_i8(db, row["operation_key"], stored_payload, allow_committed=False)
            permit = db.execute(
                "SELECT reservation_id FROM w2_positive_v1_permits WHERE operation_key=?",
                (row["operation_key"],),
            ).fetchone()
            if not permit:
                raise RuntimeError("native permit missing for I8 settlement")
            commit_result = self.backend._commit_in_connection(db, permit["reservation_id"])
            if commit_result.status not in ("COMMITTED", "ALREADY_COMMITTED"):
                raise RuntimeError(f"native I8 commit refused: {commit_result.status}")
            settled = db.execute(
                "UPDATE w2_positive_v1_capacity SET reserved=0, settled=1 "
                "WHERE operation_key=? AND reservation_id=? AND reserved=1 AND settled=0",
                (row["operation_key"], permit["reservation_id"]),
            )
            if settled.rowcount != 1:
                raise RuntimeError("positive I8 settlement metadata conflict")
            updated = db.execute(
                "UPDATE w2_positive_v1_outbox SET status='CONFIRMED', provider_receipt_json=? "
                "WHERE execution_id=? AND claim_token=? AND status IN ('PENDING','UNKNOWN')",
                (receipt_json, execution_id, token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("dispatch confirmation race")
            db.execute(
                "INSERT INTO w2_positive_v1_events (operation_key, event, at, detail) "
                "VALUES (?, 'dispatch_confirmed', ?, ?)",
                (row["operation_key"], datetime.now(timezone.utc).isoformat(), execution_id),
            )
            return "CONFIRMED"

    def receipt_context(self, claim: dict) -> dict:
        """Durable identities a connector receipt must sign. Not a send right."""
        if not isinstance(claim, dict):
            raise RuntimeError("dispatch claim required")
        execution_id = claim.get("execution_id")
        token = claim.get("claim_token")
        if not execution_id or not token:
            raise RuntimeError("dispatch claim identity missing")
        with self._tx() as db:
            row = db.execute(
                "SELECT o.execution_id, o.operation_key, o.witness_hash, o.witness_evidence_json, "
                "p.permit_id, p.reservation_id "
                "FROM w2_positive_v1_outbox o "
                "JOIN w2_positive_v1_permits p ON p.operation_key=o.operation_key "
                "WHERE o.execution_id=? AND o.claim_token=?",
                (execution_id, token),
            ).fetchone()
        if not row or not row["witness_hash"] or not row["witness_evidence_json"]:
            raise RuntimeError("receipt context unavailable")
        evidence = json.loads(row["witness_evidence_json"])
        epoch = evidence.get("record", {}).get("revision")
        if not isinstance(epoch, int):
            raise RuntimeError("witness epoch unavailable")
        return {
            "execution_id": row["execution_id"],
            "reservation_id": row["reservation_id"],
            "permit_id": row["permit_id"],
            "witness_hash": row["witness_hash"],
            "witness_epoch": epoch,
        }

    def writers(self) -> list[dict]:
        return [{
            "component": "NativePositivePermitAuthority",
            "database": self.db_path,
            "tables": [
                "w2_positive_v1_capacity", "w2_positive_v1_permits",
                "w2_positive_v1_outbox", "w2_positive_v1_events",
                "b1_native_dual_grants", "b1_native_dual_permit_links",
                "b1_native_quarantine_registry",
                "resources", "reservations",
            ],
            "i8_backend": "SQLiteConsensusBackend",
            "i8_reserve_in_transaction": "_reserve_in_connection",
            "i8_commit_in_transaction": "_commit_in_connection",
            "witness_database": getattr(self.witness, "db_path", None),
            "witness_key_custody": getattr(self.witness, "key_custody", "INJECTED"),
            "historical_g4a_modified": False,
            "key_custody": self.key_custody,
            "productive": False,
        }]

    def verify_dispatch_claim(
        self, operation_key: str, idempotency_key: str, payload: dict, claim
    ) -> bool:
        """Verify a presented dispatch claim against native outbox custody."""
        if not isinstance(claim, dict):
            return False
        try:
            if (claim.get("operation") != operation_key
                    or claim.get("idempotency_key") != idempotency_key
                    or claim.get("audience") != "kernel-dispatch"):
                return False
            with self._tx() as db:
                row = db.execute(
                    "SELECT execution_id, request_hash, claim_token, status, "
                    "witness_evidence_json, witness_hash FROM w2_positive_v1_outbox "
                    "WHERE operation_key=? AND idempotency_key=?",
                    (operation_key, idempotency_key),
                ).fetchone()
                review = db.execute(
                    "SELECT 1 FROM w2_positive_v1_events WHERE operation_key=? "
                    "AND event='dispatch_review' LIMIT 1", (operation_key,),
                ).fetchone()
                if review or not row or row["status"] not in ("PENDING", "UNKNOWN"):
                    return False
                if row["request_hash"] != canon_request_hash(payload):
                    return False
                self._require_b1_dual_binding(
                    db, operation_key, idempotency_key, payload, require_live=True,
                )
                self._require_native_i8(db, operation_key, payload, allow_committed=False)
                has_grants = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='w2_positive_g4a_v1_grants'"
                ).fetchone()
                positive = db.execute(
                    "SELECT consumed FROM w2_positive_g4a_v1_grants WHERE operation_key=?",
                    (operation_key,),
                ).fetchone() if has_grants else None
            token = row["claim_token"]
            if (claim.get("token") != token or claim.get("nonce") != token
                    or claim.get("claim_token") != token
                    or claim.get("execution_id") != row["execution_id"]):
                return False
            evidence_json = row["witness_evidence_json"]
            witness_hash = row["witness_hash"]
            if not evidence_json or not witness_hash:
                return False
            evidence = json.loads(evidence_json)
            expected_hash = hashlib.sha256(
                b"TENIR-W2-CLAIM-WITNESS-v1\0" + _canonical(evidence)
            ).hexdigest()
            if (expected_hash != witness_hash
                    or claim.get("witness_hash") != witness_hash
                    or claim.get("witness") != evidence
                    or evidence.get("record", {}).get("subject") != f"{operation_key}:{idempotency_key}"):
                return False
            if not self.witness.verify(
                evidence, subject=f"{operation_key}:{idempotency_key}",
            ):
                return False
            if positive:
                capability = claim.get("positive_send_capability")
                if positive["consumed"] != 1 or row["status"] != "UNKNOWN" or not isinstance(capability, str):
                    return False
                with self._positive_send_lock:
                    expected = self._positive_live_sends.get(row["execution_id"])
                    if not expected or not secrets.compare_digest(expected, capability):
                        return False
                    del self._positive_live_sends[row["execution_id"]]
            return True
        except Exception:
            return False
