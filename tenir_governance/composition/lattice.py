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

KB3 posture mapping (implementation → canonical):
    PASS      → NORMAL
    FLAG      → CAUTION
    HOLD      → HOLDING-FIRST
    HARD_VETO → HOLDING-FIRST (with block disposition)

These mappings are rendered at the schema conformance boundary, not here.
This module operates on implementation-level Decision enums.
"""
from __future__ import annotations

import hashlib
import json
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
    """Tamper-evident hash-chained ledger for governance decisions."""

    def __init__(self):
        self._entries: list[LedgerEntry] = []
        self._last_hash = "GENESIS"

    @property
    def entries(self) -> list[LedgerEntry]:
        return self._entries

    @property
    def entry_count(self) -> int:
        return len(self._entries)

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
        payload = json.dumps({
            "kernel": entry.kernel,
            "decision": entry.decision,
            "rationale": entry.rationale,
            "passage_id": entry.passage_id,
            "idempotency_key": entry.idempotency_key,
            "previous_hash": entry.previous_hash,
            "timestamp": entry.timestamp,
        }, sort_keys=True, separators=(",", ":"))
        entry.chain_hash = hashlib.sha256(payload.encode()).hexdigest()
        self._last_hash = entry.chain_hash
        self._entries.append(entry)
        return entry.chain_hash

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for entry in self._entries:
            if entry.previous_hash != prev:
                return False
            payload = json.dumps({
                "kernel": entry.kernel,
                "decision": entry.decision,
                "rationale": entry.rationale,
                "passage_id": entry.passage_id,
                "idempotency_key": entry.idempotency_key,
                "previous_hash": entry.previous_hash,
                "timestamp": entry.timestamp,
            }, sort_keys=True, separators=(",", ":"))
            computed = hashlib.sha256(payload.encode()).hexdigest()
            if entry.chain_hash != computed:
                return False
            prev = entry.chain_hash
        return True

    def find_by_idempotency_key(self, idempotency_key: str) -> list[LedgerEntry]:
        """Find all ledger entries for a given idempotency key."""
        return [e for e in self._entries if e.idempotency_key == idempotency_key]
