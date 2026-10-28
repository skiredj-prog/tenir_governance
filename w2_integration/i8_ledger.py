"""
PROOF-01 REPAIR lab — RECONSTRUCTED
I8 ledger + simple reconciliation bridge.
Not the original ZIP.
"""
import sqlite3
from decimal import Decimal
from threading import Lock

class I8Ledger:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = Lock()
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                operation_id TEXT PRIMARY KEY,
                reserved INTEGER,
                settled INTEGER DEFAULT 0,
                released INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()
        self._fail_next = False

    def reserve(self, op_id: str, amount: int):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO ledger (operation_id, reserved) VALUES (?, ?)",
                (op_id, amount)
            )
            self.conn.commit()

    def settle(self, op_id: str):
        with self.lock:
            if self._fail_next:
                self._fail_next = False
                raise RuntimeError("forced settle failure")
            self.conn.execute(
                "UPDATE ledger SET settled = 1 WHERE operation_id = ? AND settled = 0",
                (op_id,)
            )
            self.conn.commit()

    def release(self, op_id: str):
        with self.lock:
            cur = self.conn.execute(
                "SELECT reserved, settled FROM ledger WHERE operation_id = ?", (op_id,)
            )
            row = cur.fetchone()
            # Refuse if reservation exists and settlement has not completed
            # (covers REPAIR window). After successful settle, release is allowed
            # only for explicit FAILED paths; here we keep it strict for the lab.
            if row and row[0] and not row[1]:
                raise PermissionError("release refused from REPAIR")
            # For the reconstructed lab, also refuse if already settled
            # to match the "no undue release" invariant of the REPAIR scenario.
            if row and row[1]:
                raise PermissionError("release refused after settlement")
            self.conn.execute(
                "UPDATE ledger SET released = 1 WHERE operation_id = ?", (op_id,)
            )
            self.conn.commit()

    def abandon_pre_dispatch(self, op_id: str) -> bool:
        """Drop a reservation that never received a permit. Refuses settled rows."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT reserved, settled FROM ledger WHERE operation_id = ?", (op_id,)
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            if row[1]:
                raise PermissionError("abandon refused after settlement")
            self.conn.execute(
                "DELETE FROM ledger WHERE operation_id = ? AND settled = 0",
                (op_id,),
            )
            self.conn.commit()
            return True

    def fail_next_settle(self):
        self._fail_next = True

    def reservation_count(self, op_id: str) -> int:
        cur = self.conn.execute(
            "SELECT reserved FROM ledger WHERE operation_id = ?", (op_id,)
        )
        row = cur.fetchone()
        return 1 if row and row[0] else 0

    def net_settlement_count(self, op_id: str) -> int:
        cur = self.conn.execute(
            "SELECT settled FROM ledger WHERE operation_id = ?", (op_id,)
        )
        row = cur.fetchone()
        return 1 if row and row[0] else 0
