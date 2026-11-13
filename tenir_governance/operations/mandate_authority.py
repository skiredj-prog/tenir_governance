"""RV-002 opt-in, fail-closed mandate fence for pre-dispatch authority.

This SQLite implementation is a laboratory authority, NOT an issuer, identity
provider, credential verifier, or cross-domain production trust root. Only
trusted orchestration may register/bind/revoke mandates. Every dispatch and
revocation participating in this guarantee MUST share this same database.
A dispatch already durably claimed before revocation is outside this guarantee.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterator, Mapping


@dataclass
class MandateClaim:
    allowed: bool
    decision: str
    sequence: int | None = None
    epoch: int | None = None
    claim_succeeded: bool = False


class SQLiteMandateAuthority:
    """Serialized revocation/check/dispatch-claim ordering over one SQLite DB.

    BEGIN IMMEDIATE fences a pending native RESERVED -> DISPATCHING claim
    against any competing revocation using this authority store. The caller
    MUST keep the context open across the native durable registry transition.
    No remote revocation or already-claimed DISPATCHING operation is cancelled.
    """

    def __init__(self, path: str, *, timeout: float = 5.0):
        if not path:
            raise ValueError("A durable SQLite path is required")
        self.path = str(Path(path).resolve())
        self.timeout = timeout
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connection()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mandates (
                    mandate_id TEXT PRIMARY KEY,
                    actor_sub TEXT NOT NULL,
                    represented_user_sub TEXT NOT NULL,
                    bearer_token_ref TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    status TEXT NOT NULL CHECK (status IN ('active','revoked'))
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    operation_id TEXT PRIMARY KEY,
                    mandate_id TEXT NOT NULL REFERENCES mandates(mandate_id),
                    actor_sub TEXT NOT NULL,
                    represented_user_sub TEXT NOT NULL,
                    bearer_token_ref TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    admission_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    operation_id TEXT,
                    mandate_id TEXT,
                    epoch INTEGER,
                    decision TEXT NOT NULL
                );
            """)

    def _connection(self):
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _write(self):
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, kind, operation_id=None, mandate_id=None, epoch=None, decision=""):
        cur = db.execute(
            "INSERT INTO events (kind, operation_id, mandate_id, epoch, decision) VALUES (?,?,?,?,?)",
            (kind, operation_id, mandate_id, epoch, decision),
        )
        return int(cur.lastrowid)

    def register_mandate(
        self, *, mandate_id: str, actor_sub: str,
        represented_user_sub: str, bearer_token_ref: str,
    ) -> int:
        """Issuer-verified facts must be supplied by trusted orchestration."""
        if not all((mandate_id, actor_sub, represented_user_sub, bearer_token_ref)):
            raise ValueError("Mandate identity fields must be nonempty")
        with self._write() as db:
            db.execute(
                "INSERT INTO mandates VALUES (?,?,?,?,1,'active')",
                (mandate_id, actor_sub, represented_user_sub, bearer_token_ref),
            )
            return self._event(db, "mandate_created", mandate_id=mandate_id,
                               epoch=1, decision="ACTIVE")

    def bind_operation(
        self, *, operation_id: str, mandate_id: str, actor_sub: str,
        represented_user_sub: str, bearer_token_ref: str, payload_sha256: str,
    ) -> int:
        """Immutable operation-to-mandate binding; call only after admission."""
        if not operation_id or len(payload_sha256) != 64:
            raise ValueError("Operation ID and SHA-256 are required")
        with self._write() as db:
            m = db.execute(
                "SELECT actor_sub,represented_user_sub,bearer_token_ref,epoch,status "
                "FROM mandates WHERE mandate_id=?", (mandate_id,),
            ).fetchone()
            if m is None or m[4] != "active" or (actor_sub, represented_user_sub, bearer_token_ref) != m[:3]:
                raise ValueError("Mandate is absent, revoked, or identity does not match")
            db.execute(
                "INSERT INTO bindings VALUES (?,?,?,?,?,?,?)",
                (operation_id, mandate_id, actor_sub, represented_user_sub,
                 bearer_token_ref, payload_sha256, m[3]),
            )
            return self._event(db, "operation_bound", operation_id=operation_id,
                               mandate_id=mandate_id, epoch=m[3], decision="BOUND")

    def revoke_mandate(self, mandate_id: str) -> dict:
        """Durable revocation acknowledgement, serialized with the claim fence."""
        with self._write() as db:
            row = db.execute(
                "SELECT epoch,status FROM mandates WHERE mandate_id=?", (mandate_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown mandate")
            if row[1] == "revoked":
                found = db.execute(
                    "SELECT seq FROM events WHERE mandate_id=? AND kind='revocation_ack' "
                    "ORDER BY seq DESC LIMIT 1", (mandate_id,)
                ).fetchone()
                return {"epoch": row[0], "seq": found[0] if found else None, "status": "revoked"}
            epoch = row[0] + 1
            db.execute(
                "UPDATE mandates SET epoch=?,status='revoked' WHERE mandate_id=?",
                (epoch, mandate_id),
            )
            seq = self._event(db, "revocation_ack", mandate_id=mandate_id,
                              epoch=epoch, decision="REVOKED")
            return {"epoch": epoch, "seq": seq, "status": "revoked"}

    @contextmanager
    def claim_fence(
        self, *, operation_id: str, actor_sub: str,
        payload_sha256: str, authority_context: Mapping[str, str] | None,
    ) -> Iterator[MandateClaim]:
        """Keep this context open across the native registry dispatch claim.

        A successful claim before revocation is permitted to finish. Revocation
        acknowledged earlier must cause DENY_REVOKED. SQLite failures must be
        caught by the Governor as DENY_AUTHORITY_UNAVAILABLE with zero provider calls.
        """
        with self._write() as db:
            binding = db.execute(
                "SELECT mandate_id,actor_sub,represented_user_sub,bearer_token_ref,"
                "payload_sha256,admission_epoch FROM bindings WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            epoch = None
            mandate_id = binding[0] if binding else None
            decision = "DENY_NO_BINDING"
            if binding is not None:
                m = db.execute(
                    "SELECT actor_sub,represented_user_sub,bearer_token_ref,epoch,status "
                    "FROM mandates WHERE mandate_id=?", (mandate_id,),
                ).fetchone()
                if m is None:
                    decision = "DENY_MANDATE_UNKNOWN"
                elif m[4] != "active":
                    epoch, decision = m[3], "DENY_REVOKED"
                elif m[3] != binding[5]:
                    epoch, decision = m[3], "DENY_EPOCH_ADVANCED"
                elif actor_sub != binding[1] or m[0] != binding[1]:
                    decision = "DENY_ACTOR_MISMATCH"
                elif payload_sha256 != binding[4]:
                    decision = "DENY_PAYLOAD_MISMATCH"
                elif m[1] != binding[2] or m[2] != binding[3]:
                    decision = "DENY_MANDATE_IDENTITY_CHANGED"
                elif not isinstance(authority_context, Mapping):
                    decision = "DENY_MISSING_CONTEXT"
                elif (authority_context.get("mandate_id") != binding[0]
                      or authority_context.get("actor_sub") != binding[1]
                      or authority_context.get("represented_user_sub") != binding[2]
                      or authority_context.get("bearer_token_ref") != binding[3]):
                    decision = "DENY_CONTEXT_MISMATCH"
                else:
                    epoch, decision = m[3], "ALLOW_CLAIM"
            seq = self._event(
                db, "release_check", operation_id=operation_id,
                mandate_id=mandate_id, epoch=epoch, decision=decision,
            )
            claim = MandateClaim(
                allowed=decision == "ALLOW_CLAIM",
                decision=decision, sequence=seq, epoch=epoch,
            )
            try:
                yield claim
            finally:
                if claim.claim_succeeded:
                    self._event(db, "dispatch_claim", operation_id=operation_id,
                                mandate_id=mandate_id, epoch=epoch,
                                decision="DISPATCHING")

    def audit_events(self, operation_id: str | None = None) -> list[dict]:
        """Local lab evidence; not a cryptographically independent audit."""
        with closing(self._connection()) as db:
            if operation_id is None:
                rows = db.execute(
                    "SELECT seq,kind,operation_id,mandate_id,epoch,decision FROM events ORDER BY seq"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT seq,kind,operation_id,mandate_id,epoch,decision FROM events "
                    "WHERE operation_id=? ORDER BY seq", (operation_id,)
                ).fetchall()
            keys = ("seq", "kind", "operation_id", "mandate_id", "epoch", "decision")
            return [dict(zip(keys, row)) for row in rows]
