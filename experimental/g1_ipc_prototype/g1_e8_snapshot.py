"""Canonical G1-E8 LAB business-state snapshot.

Recomputes all business and idempotency rows under the caller's SQLite read
transaction. This is NOT a provider-effect receipt, a G4-A coverage proof, or
a defense against an attacker who controls the executing G1 process.
"""
from e8_lab import digest

# Explicit, strict table/column allowlist. Missing/modified schema fails closed.
TABLES = {
    "g1_local_commits": (
        "realm", "epoch", "transaction_id", "payload_digest", "request_id",
    ),
    "g1_idempotency_ledger": (
        "request_id", "epoch", "worker_uid", "operation", "request_digest",
        "status", "result_digest", "created_at",
    ),
}

# Canonicalization revision intentionally included in the attested state.
SCHEMA = "g1-e8-business-snapshot-v1"


class BusinessStateUnverifiable(ValueError):
    pass


def snapshot(con, *, proposed_commit=None, proposed_idempotency=None):
    """Return canonical snapshot of *all* scoped demo business/ledger tables.

    Optional proposed rows predict the post-transaction digest BEFORE remote
    witness pre-authorization. Prediction never writes to SQLite. The caller
    MUST re-read and compare after the inserts, before local COMMIT.
    """
    if (proposed_commit is None) != (proposed_idempotency is None):
        raise ValueError("proposed rows must be provided together")
    state = {"schema": SCHEMA, "tables": {}}
    proposed = {
        "g1_local_commits": proposed_commit,
        "g1_idempotency_ledger": proposed_idempotency,
    }
    for table, columns in TABLES.items():
        # SQL identifiers are static constants, never constructed from input.
        info = con.execute(f"PRAGMA table_info({table})").fetchall()
        actual_columns = tuple(item[1] for item in info)
        if actual_columns != columns:
            raise BusinessStateUnverifiable(f"schema mismatch: {table}")
        actual_schema = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not actual_schema or not actual_schema[0]:
            raise BusinessStateUnverifiable(f"missing table: {table}")
        sql_columns = ", ".join(columns)
        rows = [list(row) for row in con.execute(f"SELECT {sql_columns} FROM {table}").fetchall()]
        if proposed[table] is not None:
            extra = list(proposed[table])
            if len(extra) != len(columns):
                raise BusinessStateUnverifiable(f"invalid predicted row: {table}")
            rows.append(extra)
        # Canonical sorting is by serialized row, independent of SQLite query plan.
        rows.sort(key=lambda r: tuple(str(v) for v in r))
        state["tables"][table] = {
            "columns": list(columns),
            "definition": actual_schema[0],
            "rows": rows,
        }
    return state


def business_digest(con, **kwargs):
    return digest(snapshot(con, **kwargs))
