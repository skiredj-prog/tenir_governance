"""External monotone anchor. Not an independent custodian.

A same-UID process that can also roll back this file defeats it.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


class ExternalMonotoneAnchor:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._tx() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS anchor_events (
                     seq INTEGER PRIMARY KEY AUTOINCREMENT,
                     subject TEXT NOT NULL,
                     revision INTEGER NOT NULL,
                     execution_id TEXT NOT NULL,
                     claim_token TEXT NOT NULL,
                     noted_at TEXT NOT NULL
                   )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS anchor_subject ON anchor_events(subject, revision)"
            )

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def note(self, subject: str, revision: int, execution_id: str, claim_token: str) -> int:
        if not subject or revision < 1 or not execution_id or not claim_token:
            raise RuntimeError("anchor subject and identity required")
        with self._tx() as db:
            last = db.execute(
                "SELECT revision, execution_id, claim_token FROM anchor_events "
                "WHERE subject=? ORDER BY revision DESC LIMIT 1",
                (subject,),
            ).fetchone()
            if last and revision < last["revision"]:
                raise RuntimeError("external anchor anti-downgrade")
            if last and revision == last["revision"]:
                if last["execution_id"] != execution_id or last["claim_token"] != claim_token:
                    raise RuntimeError("external anchor identity conflict")
                return last["revision"]
            db.execute(
                "INSERT INTO anchor_events (subject, revision, execution_id, claim_token, noted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (subject, revision, execution_id, claim_token,
                 datetime.now(timezone.utc).isoformat()),
            )
            return revision

    def latest(self, subject: str):
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return db.execute(
                "SELECT revision, execution_id, claim_token FROM anchor_events "
                "WHERE subject=? ORDER BY revision DESC LIMIT 1",
                (subject,),
            ).fetchone()

    def require_not_behind(self, subject: str, local_revision: int) -> None:
        last = self.latest(subject)
        if last and local_revision < last["revision"]:
            raise RuntimeError("local state is behind external anchor")
