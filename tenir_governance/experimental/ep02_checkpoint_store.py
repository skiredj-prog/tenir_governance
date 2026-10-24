"""Experimental durable EP-02 checkpoint, single-host POSIX file locking.

Only stores a monotone trust high-water mark. NOT a distributed authority,
nor a transactional guard for external commits.
"""
from dataclasses import asdict
from pathlib import Path
import fcntl
import json
import os
import tempfile

from tenir_governance.experimental.ep02_rotation import TrustCheckpoint, validate_successor


class CheckpointCorrupted(RuntimeError):
    pass


class CheckpointStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")

    def _read(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if set(data) != {"revision", "snapshot_fingerprint"}:
                raise ValueError("invalid fields")
            revision, digest = data["revision"], data["snapshot_fingerprint"]
            if type(revision) is not int or revision < 1 or not isinstance(digest, str) or len(digest) != 64:
                raise ValueError("invalid checkpoint")
            bytes.fromhex(digest)
            return TrustCheckpoint(revision, digest)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise CheckpointCorrupted("checkpoint unreadable; fail closed") from error

    def get(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._read()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def advance(self, candidate, *, roots, bootstrap=False):
        """Persist before reporting success; a later error may be ambiguous."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                previous = self._read()
                if previous is None:
                    if not bootstrap:
                        return False, "TRUST_CHECKPOINT_NOT_INITIALIZED"
                    from hashlib import sha256
                    from tenir_governance.experimental.ep02_signed_trust import (
                        bound_snapshot_bytes, verify_bound_snapshot,
                    )
                    if not verify_bound_snapshot(candidate, root_keys=roots):
                        return False, "UNVERIFIED_INITIAL_SNAPSHOT"
                    current = TrustCheckpoint(
                        candidate.state.revision,
                        sha256(bound_snapshot_bytes(candidate)).hexdigest(),
                    )
                    reason = "BOOTSTRAPPED"
                else:
                    ok, reason, current = validate_successor(
                        previous, candidate, roots=roots)
                    if not ok:
                        return False, reason
                    if reason == "UNCHANGED":
                        return True, reason
                self._write_locked(current)
                return True, reason
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_locked(self, current):
        """Caller must own the exclusive checkpoint file lock."""
        payload = json.dumps(asdict(current), sort_keys=True).encode()
        fd, tmp = tempfile.mkstemp(prefix=".checkpoint-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            dirfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
