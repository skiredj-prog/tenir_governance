# Profile: agentic-governance-profile-v1.0.0
# Compliance class: profile-bound
# This module is an extension on top of KB3 and TENIR 2.0 schema 1.2.
# It is NOT canonical, NOT doctrinal. It carries implementation-level
# governance enforcement authority only.
"""Decision lattice and hash-chained ledger for two-kernel composition.

The decision lattice composes a continuous risk-monitor decision with a
discrete invariant-enforcer decision. Fail-closed semantics: if either
kernel returns no decision (exception, missing data), the result is HOLD,
never PASS.

Execution verdict and posture are distinct fields. They are computed
and returned separately; this module does not map one onto the other.
    Verdict (what is): PASS / FLAG / HOLD / HARD_VETO
    Posture (what we do): NORMAL / CAUTION / HOLDING-FIRST
schema_adapter.py returns posture in its own field.
This module operates on implementation-level Decision enums.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .decisions import Decision, ExecutionStatus, CheckType
from .policy import CompositionPolicy


# ── Decision Lattice ────────────────────────────────────────────────────

_SEVERITY = {
    Decision.PASS: 0,
    Decision.FLAG: 1,
    Decision.HOLD: 2,
    Decision.HARD_VETO: 3,
}


def compose(
    scoring_decision: str | None,
    membrane_decision: str | None,
    scoring_ces: str | None = None,
    membrane_checks: list[dict] | None = None,
    budget_utilization: float | None = None,
    budget_in_scope: bool = False,
    policy: CompositionPolicy | None = None,
) -> tuple[str, str]:
    """Compose two kernel decisions using the decision lattice.

    Fail-closed semantics (per review 2 §2):
    - If either kernel decision is None (exception, missing data), the result
      is HOLD, never PASS.
    - If budget is in scope but utilization is None (missing/stale measurement),
      and a FLAG is present, escalate to HOLD rather than silently proceeding.

    Returns:
        (composed_decision, rationale)
    """
    p = policy or CompositionPolicy()

    # Fail-closed: None decisions → HOLD
    if scoring_decision is None and membrane_decision is None:
        return Decision.HOLD.value, "Both kernels returned no decision — fail-closed HOLD."
    if scoring_decision is None:
        m = Decision(membrane_decision)
        if m == Decision.HARD_VETO:
            return Decision.HARD_VETO.value, "Membrane HARD_VETO (scoring kernel unavailable)."
        return Decision.HOLD.value, "Scoring kernel returned no decision — fail-closed HOLD."
    if membrane_decision is None:
        s = Decision(scoring_decision)
        if s == Decision.HARD_VETO:
            return Decision.HARD_VETO.value, "Scoring HARD_VETO (membrane kernel unavailable)."
        return Decision.HOLD.value, "Membrane kernel returned no decision — fail-closed HOLD."

    s = Decision(scoring_decision)
    m = Decision(membrane_decision)

    # Rule 1: HARD_VETO from either → HARD_VETO (retain both reasons)
    if s == Decision.HARD_VETO or m == Decision.HARD_VETO:
        reasons = []
        if s == Decision.HARD_VETO:
            reasons.append("scoring kernel: conclusive risk")
        if m == Decision.HARD_VETO:
            reasons.append("membrane kernel: mandatory invariant violated")
        if m == Decision.HOLD:
            reasons.append("membrane HOLD: pending reconciliation (retained)")
        if s == Decision.HOLD:
            reasons.append("scoring HOLD (retained)")
        return Decision.HARD_VETO.value, "Blocked — " + "; ".join(reasons) + "."

    # Rule 2: HOLD from membrane → HOLD
    if m == Decision.HOLD:
        return Decision.HOLD.value, "Membrane requests hold: pending evidence or reconciliation."

    # Rule 2b: HOLD from scoring → HOLD. The most restrictive disposition
    # governs (RFC-0 §4); a scoring HOLD (e.g. insufficient evidence) is never
    # dropped in favour of a membrane PASS or FLAG.
    if s == Decision.HOLD:
        return Decision.HOLD.value, "Scoring requests hold: insufficient evidence."

    # Rule 3: FLAG + near-limit budget → HOLD (escalation)
    threshold = p.budget_escalation_threshold
    if s == Decision.FLAG or m == Decision.FLAG:
        if budget_in_scope and budget_utilization is None:
            flag_source = "scoring" if s == Decision.FLAG else "membrane"
            return Decision.HOLD.value, (
                f"{flag_source} FLAG + indeterminate budget measurement: "
                "fail-closed HOLD pending evidence."
            )
        if budget_utilization is not None and budget_utilization >= threshold:
            flag_source = "scoring" if s == Decision.FLAG else "membrane"
            return Decision.HOLD.value, (
                f"{flag_source} FLAG + budget utilization {budget_utilization:.0%} ≥ {threshold:.0%}: "
                "escalated to HOLD pending evidence."
            )

    # Rule 4: FLAG from either → FLAG
    if s == Decision.FLAG or m == Decision.FLAG:
        if s == Decision.FLAG and m == Decision.FLAG:
            return Decision.FLAG.value, "Both kernels flag: non-blocking risk warning."
        if s == Decision.FLAG:
            return Decision.FLAG.value, "Scoring kernel flags: risk warning."
        return Decision.FLAG.value, "Membrane kernel flags: risk warning."

    # Rule 5: PASS from both → PASS
    return Decision.PASS.value, "Both kernels passed."


# ── Hash-Chained Ledger ─────────────────────────────────────────────────

@dataclass
class LedgerEntry:
    """A single entry in the hash-chained evidence ledger."""
    entry_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    previous_hash: str = "GENESIS"
    chain_hash: str = ""
    kernel: str = ""
    decision: str = ""
    rationale: str = ""
    passage_id: str = ""
    idempotency_key: str = ""
    scoring_result: dict = field(default_factory=dict)
    membrane_result: dict = field(default_factory=dict)
    composed_decision: str = ""


class HashChainedLedger:
    """Tamper-evident hash-chained ledger for governance decisions.

    Two verifications (candidate 5.7.7, author's decision of 2026-11-23,
    verify_chain option A):

    * ``verify_chain()`` is the full verification: every entry is re-hashed
      and every link is checked, from GENESIS to the last entry. O(N). It
      detects any alteration of any entry, old or new. Call it on demand (an
      audit, a manifest check, the end of a run).
    * ``verify_new_entries()`` is the incremental verification: it re-hashes
      only the entries appended since the previous verification, checks that
      they link to each other and to the verified prefix, and checks that the
      anchor (the last verified entry) still carries the hash it had. O(k)
      for k new entries, so constant per decision. It is what a decision
      result reports as ``ledger_valid``. Declared limit: an alteration of an
      entry inside the already verified prefix, other than its anchor, is not
      seen by the incremental check until the next full verification, which
      is why that one exists.

    Up to 5.7.6 every decision ran the full verification, which made the cost
    of a decision grow with the size of the ledger (measured on this
    candidate, 2026-11-23: 97 % of the time of ``adjudicate`` at 1000 held
    operations).

    Concurrency (candidate 5.7.7, project owner's review of 2026-11-23, 17:37
    UTC): ``append`` and both verifications run under one re-entrant lock.
    Up to 5.7.6 and in the earlier states of this candidate ``append`` read
    and wrote ``_last_hash`` without a lock, so two threads appending at once
    could fork the chain (measured: 8 threads, 25 decisions each, the chain
    failed to verify in 9 runs out of 12). A forked chain is not an
    alteration, but ``verify_chain()`` cannot tell the two apart, and since
    the candidate verifies the chain before every dispatch claim a fork would
    have blocked every dispatch.

    Declared limits (unchanged since 5.7.0): the ledger lives in memory, in
    one process, and is not persisted with the durable operation registry;
    the chain proves internal consistency (every entry hashes as recorded and
    links to its predecessor), not authenticity: it is bound to no key, so an
    entry rewritten and the chain re-hashed from it verifies. ``entries``
    gives the live list; a caller that mutates it alters the ledger.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []
        self._last_hash = "GENESIS"
        # Verified prefix: number of entries verified so far, and the chain
        # hash of the last of them (GENESIS when none).
        self._verified_count = 0
        self._verified_hash = "GENESIS"

    # The lock is per instance and never copied or serialised: a deep copy or
    # an unpickled ledger gets a fresh one (a snapshot of the ledger is an
    # audit object; the REG harness takes one to check the manifest).
    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_lock", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.RLock()

    @property
    def entries(self) -> list[LedgerEntry]:
        return self._entries

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def verified_count(self) -> int:
        """Entries covered by the last verification (full or incremental)."""
        return self._verified_count

    @staticmethod
    def _entry_hash(entry: LedgerEntry) -> str:
        payload = json.dumps({
            "kernel": entry.kernel,
            "decision": entry.decision,
            "rationale": entry.rationale,
            "passage_id": entry.passage_id,
            "idempotency_key": entry.idempotency_key,
            "previous_hash": entry.previous_hash,
            "timestamp": entry.timestamp,
            "scoring_result": entry.scoring_result,
            "membrane_result": entry.membrane_result,
            "composed_decision": entry.composed_decision,
        }, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def append(
        self,
        kernel: str,
        decision: str,
        rationale: str,
        passage_id: str = "",
        idempotency_key: str = "",
        scoring_result: dict | None = None,
        membrane_result: dict | None = None,
        composed_decision: str = "",
    ) -> str:
        with self._lock:
            entry = LedgerEntry(
                previous_hash=self._last_hash,
                kernel=kernel,
                decision=decision,
                rationale=rationale,
                passage_id=passage_id,
                idempotency_key=idempotency_key,
                scoring_result=scoring_result or {},
                membrane_result=membrane_result or {},
                composed_decision=composed_decision,
            )
            entry.chain_hash = self._entry_hash(entry)
            self._last_hash = entry.chain_hash
            self._entries.append(entry)
            return entry.chain_hash

    def _verify_range(self, start: int, prev: str) -> bool:
        """Check links and hashes of entries[start:] against ``prev``."""
        for entry in self._entries[start:]:
            if entry.previous_hash != prev:
                return False
            if entry.chain_hash != self._entry_hash(entry):
                return False
            prev = entry.chain_hash
        return True

    def verify_chain(self) -> bool:
        """Full verification, from GENESIS: every entry, every link. O(N).
        Internal consistency only (see the class docstring). Holds the ledger
        lock for its whole duration: no append lands while it runs."""
        with self._lock:
            ok = self._verify_range(0, "GENESIS")
            if ok:
                self._verified_count = len(self._entries)
                self._verified_hash = self._entries[-1].chain_hash if self._entries else "GENESIS"
            return ok

    def verify_new_entries(self) -> bool:
        """Incremental verification of the entries appended since the last
        verification, anchored on the verified prefix. O(k)."""
        with self._lock:
            count = self._verified_count
            if len(self._entries) < count:
                return False  # the verified prefix has been truncated
            if count:
                anchor = self._entries[count - 1]
                if anchor.chain_hash != self._verified_hash or anchor.chain_hash != self._entry_hash(anchor):
                    return False  # the anchor was altered or replaced
            ok = self._verify_range(count, self._verified_hash)
            if ok:
                self._verified_count = len(self._entries)
                self._verified_hash = self._entries[-1].chain_hash if self._entries else "GENESIS"
            return ok

    def find_by_idempotency_key(self, idempotency_key: str) -> list[LedgerEntry]:
        """Find all ledger entries for a given idempotency key."""
        return [e for e in self._entries if e.idempotency_key == idempotency_key]
