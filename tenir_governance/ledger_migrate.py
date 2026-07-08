"""
TENIR Ledger Migration Tool
============================
SPRINT 11 — Legacy Label Migration

Rewrites existing JSONL ledgers to replace deprecated labels with canonical
ones per TENIR_public_safe_lexicon.csv rename-now directives:

    SCHIZOPHRENIA        → SIGNAL_CONFLICT
    SCHIZOPHRENIA_ALERT  → SIGNAL_CONFLICT_ALERT
    WHALE_RESONANCE      → DEEP_PATTERN_SIGNAL

CRITICAL:
  This tool does NOT invalidate hash chains. It creates a NEW chain rooted
  at a migration genesis hash and leaves the original ledger untouched.
  The original is preserved at <path>.pre-migration.jsonl for forensic
  replay.

Usage:
    # Dry-run report (no writes)
    python -m tenir_governance.ledger_migrate --dry-run audit/ledger.jsonl

    # Apply migration
    python -m tenir_governance.ledger_migrate audit/ledger.jsonl

    # Verify a migrated ledger
    python -m tenir_governance.ledger_migrate --verify audit/ledger.jsonl

The migration entry type is `legacy_label_migration` and carries:
  - original ledger path
  - original final hash (the root of the pre-migration chain)
  - count and list of renames applied
  - migration timestamp
  - migration tool version
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .nomenclature import CESStateNames, WSFrameTypes


MIGRATION_TOOL_VERSION = "1.0.0"
GENESIS_HASH = "GENESIS"


@dataclass
class MigrationReport:
    source_path: str
    entries_scanned: int = 0
    entries_rewritten: int = 0
    renames_by_key: Dict[str, int] = field(default_factory=dict)
    original_final_hash: Optional[str] = None
    migrated_final_hash: Optional[str] = None

    def summary(self) -> str:
        lines = [
            f"TENIR Ledger Migration — {self.source_path}",
            f"  Entries scanned:    {self.entries_scanned}",
            f"  Entries rewritten:  {self.entries_rewritten}",
        ]
        for key, count in sorted(self.renames_by_key.items()):
            lines.append(f"  Rename [{key}]: {count}")
        if self.original_final_hash:
            lines.append(f"  Pre-migration chain tail:  {self.original_final_hash[:16]}…")
        if self.migrated_final_hash:
            lines.append(f"  Post-migration chain tail: {self.migrated_final_hash[:16]}…")
        return "\n".join(lines)


# ─── RENAME RULES ────────────────────────────────────────────────────────────

# Keys whose values may contain legacy CES state names
_CES_VALUE_KEYS = {"ces_state", "state", "ces", "classification"}

# Keys whose values may contain legacy WS frame types
_FRAME_VALUE_KEYS = {"frame_type", "type", "event_type"}


def _rewrite_value(key: str, value) -> Tuple[object, Optional[str]]:
    """
    If value matches a known legacy label, rewrite it.
    Returns (new_value, rename_key_for_report).
    """
    if not isinstance(value, str):
        return value, None

    if key in _CES_VALUE_KEYS:
        new = CESStateNames.normalize(value)
        if new != value:
            return new, f"ces:{value}→{new}"

    if key in _FRAME_VALUE_KEYS:
        new = WSFrameTypes.normalize(value)
        if new != value:
            return new, f"frame:{value}→{new}"

    # Also check for legacy tokens inside free-form text fields
    if key in {"rationale", "message", "description"}:
        if "SCHIZOPHRENIA" in value:
            return value.replace("SCHIZOPHRENIA", "SIGNAL_CONFLICT"), f"text:SCHIZOPHRENIA"
        if "WHALE_RESONANCE" in value:
            return value.replace("WHALE_RESONANCE", "DEEP_PATTERN_SIGNAL"), f"text:WHALE_RESONANCE"

    return value, None


def _walk_and_rewrite(payload, report: MigrationReport) -> object:
    """Recursively walk a dict/list payload and rewrite legacy labels."""
    if isinstance(payload, dict):
        new = {}
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                new[k] = _walk_and_rewrite(v, report)
            else:
                new_v, rename = _rewrite_value(k, v)
                if rename:
                    report.renames_by_key[rename] = report.renames_by_key.get(rename, 0) + 1
                new[k] = new_v
        return new
    if isinstance(payload, list):
        return [_walk_and_rewrite(item, report) for item in payload]
    return payload


# ─── HASH CHAIN HELPERS ──────────────────────────────────────────────────────

def _compute_chain_hash(previous_hash: str, payload: Dict) -> str:
    clean = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    chain_input = f"{previous_hash}|{clean}".encode("utf-8")
    return hashlib.sha256(chain_input).hexdigest()


# ─── CORE MIGRATION ──────────────────────────────────────────────────────────

def migrate_ledger(source: Path, dry_run: bool = False) -> MigrationReport:
    """
    Apply the legacy label migration to a JSONL ledger.

    Creates:
      <source>.pre-migration.jsonl — backup of original
      <source>                     — new file with migration-genesis chain
    """
    if not source.exists():
        raise FileNotFoundError(f"Ledger not found: {source}")

    report = MigrationReport(source_path=str(source))

    # ── Read original ─────────────────────────────────────────────────────────
    with source.open("r", encoding="utf-8") as f:
        original_lines = [line.strip() for line in f if line.strip()]

    report.entries_scanned = len(original_lines)

    if not original_lines:
        return report

    # Find the original final hash (for forensic continuity)
    try:
        last_entry = json.loads(original_lines[-1])
        report.original_final_hash = last_entry.get("chain_hash", last_entry.get("entry_hash"))
    except json.JSONDecodeError:
        pass

    # ── Rewrite payloads ──────────────────────────────────────────────────────
    new_entries: List[Dict] = []

    # First entry: migration-genesis marker
    migration_genesis = {
        "type": "legacy_label_migration",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_version": MIGRATION_TOOL_VERSION,
        "source_path": str(source),
        "original_final_hash": report.original_final_hash,
        "rename_rules": [
            "SCHIZOPHRENIA → SIGNAL_CONFLICT",
            "SCHIZOPHRENIA_ALERT → SIGNAL_CONFLICT_ALERT",
            "WHALE_RESONANCE → DEEP_PATTERN_SIGNAL",
        ],
    }
    genesis_hash = _compute_chain_hash(GENESIS_HASH, migration_genesis)
    new_entries.append({
        "previous_hash": GENESIS_HASH,
        "chain_hash": genesis_hash,
        "timestamp": migration_genesis["timestamp"],
        "payload": migration_genesis,
    })

    prev_hash = genesis_hash

    # Migrate all original entries
    for line in original_lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip malformed

        payload = entry.get("payload", entry)
        new_payload = _walk_and_rewrite(payload, report)

        if new_payload != payload:
            report.entries_rewritten += 1

        new_hash = _compute_chain_hash(prev_hash, new_payload)
        new_entries.append({
            "previous_hash": prev_hash,
            "chain_hash": new_hash,
            "timestamp": entry.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "payload": new_payload,
            "legacy_hash": entry.get("chain_hash") if isinstance(entry, dict) else None,
        })
        prev_hash = new_hash

    report.migrated_final_hash = prev_hash

    # ── Write new ledger (unless dry-run) ─────────────────────────────────────
    if not dry_run:
        backup = source.with_suffix(source.suffix + ".pre-migration")
        shutil.copy2(source, backup)

        with source.open("w", encoding="utf-8") as f:
            for entry in new_entries:
                f.write(json.dumps(entry) + "\n")

    return report


def verify_migrated_ledger(path: Path) -> Tuple[bool, str]:
    """Verify a migrated ledger's hash chain."""
    prev = GENESIS_HASH
    count = 0
    has_migration_genesis = False

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)

            stored_prev = entry.get("previous_hash")
            if stored_prev != prev:
                return False, f"Broken chain at entry {count}: expected prev {prev[:16]}… got {stored_prev}"

            payload = entry.get("payload", {})
            computed = _compute_chain_hash(prev, payload)
            stored = entry.get("chain_hash")
            if stored != computed:
                return False, f"Hash mismatch at entry {count}"

            if count == 0 and payload.get("type") == "legacy_label_migration":
                has_migration_genesis = True

            prev = stored
            count += 1

    if not has_migration_genesis:
        return False, "No migration genesis entry found (not a migrated ledger)"

    return True, f"Valid migrated ledger: {count} entries, chain intact"


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="TENIR Ledger Migration Tool — rename legacy labels"
    )
    parser.add_argument("path", type=Path, help="Path to JSONL ledger file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be migrated without writing")
    parser.add_argument("--verify", action="store_true",
                        help="Verify chain integrity of a migrated ledger")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON report")
    args = parser.parse_args(argv)

    if args.verify:
        ok, msg = verify_migrated_ledger(args.path)
        if args.json:
            print(json.dumps({"valid": ok, "message": msg}))
        else:
            print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1

    try:
        report = migrate_ledger(args.path, dry_run=args.dry_run)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "source":              report.source_path,
            "entries_scanned":     report.entries_scanned,
            "entries_rewritten":   report.entries_rewritten,
            "renames":             report.renames_by_key,
            "original_final_hash": report.original_final_hash,
            "migrated_final_hash": report.migrated_final_hash,
            "dry_run":             args.dry_run,
        }, indent=2))
    else:
        print(report.summary())
        if args.dry_run:
            print("\n(dry-run — no files written)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
