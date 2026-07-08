from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on non-Windows hosts
    import fcntl


class LedgerIntegrityError(ValueError):
    """Raised when the hash chain or ledger encoding is invalid."""


class LedgerLockError(RuntimeError):
    """Raised when the ledger writer lock cannot be acquired in time."""


class HashChainedLedger:
    """Append-only JSONL ledger with verified hash chaining."""

    GENESIS_HASH = "GENESIS"
    LOCK_TIMEOUT_SECONDS = 5.0
    LOCK_POLL_SECONDS = 0.05
    LOCK_BYTES = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._last_hash = self._recover_last_hash()

    @property
    def last_hash(self) -> str:
        return self._last_hash

    @staticmethod
    def _clean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        return json.loads(json.dumps(payload, sort_keys=True, default=str))

    @classmethod
    def _compute_chain_hash(cls, previous_hash: str, payload: Dict[str, Any]) -> str:
        clean_payload = cls._clean_payload(payload)
        canonical = json.dumps(clean_payload, sort_keys=True, separators=(",", ":"))
        chain_input = f"{previous_hash}|{canonical}".encode("utf-8")
        return hashlib.sha256(chain_input).hexdigest()

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as handle:
            if self._lock_path.stat().st_size < self.LOCK_BYTES:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())

            deadline = time.monotonic() + self.LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, self.LOCK_BYTES)
                    else:  # pragma: no cover - exercised on non-Windows hosts
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise LedgerLockError(
                            f"timed out acquiring ledger writer lock for {self.path}"
                        ) from exc
                    time.sleep(self.LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, self.LOCK_BYTES)
                    else:  # pragma: no cover - exercised on non-Windows hosts
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def append(self, payload: Dict[str, Any]) -> str:
        clean_payload = self._clean_payload(payload)
        with self._writer_lock():
            current_last_hash = self.verify_chain()
            chain_hash = self._compute_chain_hash(current_last_hash, clean_payload)
            entry = {
                "previous_hash": current_last_hash,
                "chain_hash": chain_hash,
                "payload": clean_payload,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._last_hash = chain_hash
            return chain_hash

    def iter_entries(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return iter(())

        def generator() -> Iterator[Dict[str, Any]]:
            deadline = time.monotonic() + self.LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    handle = self.path.open("r", encoding="utf-8")
                    break
                except PermissionError as exc:
                    if time.monotonic() >= deadline:
                        raise LedgerLockError(
                            f"timed out opening ledger for read: {self.path}"
                        ) from exc
                    time.sleep(self.LOCK_POLL_SECONDS)

            with handle:
                for line_number, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError as exc:
                        raise LedgerIntegrityError(
                            f"ledger corruption detected in {self.path} at line {line_number}"
                        ) from exc
                    yield parsed

        return generator()

    def verify_chain(self) -> str:
        if not self.path.exists():
            return self.GENESIS_HASH

        previous_hash = self.GENESIS_HASH
        last_hash = self.GENESIS_HASH

        for line_number, entry in enumerate(self.iter_entries(), start=1):
            if not isinstance(entry, dict):
                raise LedgerIntegrityError(
                    f"ledger entry at line {line_number} must be a JSON object"
                )
            if {"previous_hash", "chain_hash", "payload"} - entry.keys():
                raise LedgerIntegrityError(
                    f"ledger entry at line {line_number} is missing required keys"
                )
            if entry["previous_hash"] != previous_hash:
                raise LedgerIntegrityError(
                    f"previous_hash mismatch at line {line_number}"
                )
            expected_hash = self._compute_chain_hash(previous_hash, entry["payload"])
            if entry["chain_hash"] != expected_hash:
                raise LedgerIntegrityError(
                    f"chain_hash mismatch at line {line_number}"
                )
            previous_hash = entry["chain_hash"]
            last_hash = entry["chain_hash"]

        return last_hash

    def _recover_last_hash(self) -> str:
        return self.verify_chain()
