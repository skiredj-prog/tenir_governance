"""Small cross-platform advisory file locks for single-host coordination."""

from __future__ import annotations

import os
import threading


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


class _LockedHandle:
    """Release the process-local lock when the OS lock handle closes."""

    def __init__(self, handle, thread_lock):
        self._handle = handle
        self._thread_lock = thread_lock

    def close(self):
        try:
            self._handle.close()
        finally:
            self._thread_lock.release()


def _thread_lock_for(path: str):
    key = os.path.normcase(os.path.abspath(path))
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.RLock())


def acquire_file_lock(path: str):
    """Open and exclusively lock a stable lock file until its handle closes.

    POSIX uses ``flock``. Windows uses a one-byte ``msvcrt`` range lock.
    The lock file is intentionally separate from the atomically replaced data
    file so all cooperating processes lock the same inode/path.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    process_lock = _thread_lock_for(path)
    process_lock.acquire()
    if os.name == "nt":
        import msvcrt

        handle = None
        try:
            handle = open(path, "a+b", buffering=0)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return _LockedHandle(handle, process_lock)
        except BaseException:
            if handle is not None:
                handle.close()
            process_lock.release()
            raise

    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - supported runtimes have one backend
        raise RuntimeError("Cross-process file locking is unavailable on this platform.") from exc
    handle = None
    try:
        handle = open(path, "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return _LockedHandle(handle, process_lock)
    except BaseException:
        if handle is not None:
            handle.close()
        process_lock.release()
        raise
