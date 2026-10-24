"""EP-02 experimental single-host authoritative compare-and-swap.

This is a *local* reference backend, NOT an inter-host consensus service.
Each CAS serializes under a POSIX file lock, verifies the root signature,
and durably persists the successor before acknowledging success.
"""
from dataclasses import dataclass
from enum import Enum
import fcntl

from tenir_governance.experimental.ep02_checkpoint_store import CheckpointStore
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint, validate_successor


class CASResult(str, Enum):
    APPLIED = "APPLIED"
    UNCHANGED = "UNCHANGED"
    CONFLICT = "CONFLICT"
    NOT_INITIALIZED = "NOT_INITIALIZED"


@dataclass(frozen=True)
class CASResponse:
    result: CASResult
    checkpoint: TrustCheckpoint | None


class LocalAnchorCAS(CheckpointStore):
    def compare_and_swap(self, expected: TrustCheckpoint, successor, *, roots):
        """Require exact expected checkpoint before advancing.

        Storage errors propagate: caller MUST treat them as outcome UNKNOWN
        and query the durable record rather than retry blindly.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                actual = self._read()
                if actual is None:
                    return CASResponse(CASResult.NOT_INITIALIZED, None)
                if actual != expected:
                    return CASResponse(CASResult.CONFLICT, actual)
                ok, reason, candidate = validate_successor(
                    actual, successor, roots=roots)
                if not ok:
                    return CASResponse(CASResult.CONFLICT, actual)
                if reason == "UNCHANGED":
                    return CASResponse(CASResult.UNCHANGED, actual)
                self._write_locked(candidate)
                return CASResponse(CASResult.APPLIED, candidate)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
