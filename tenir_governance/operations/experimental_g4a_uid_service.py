"""Experimental Linux UID-separated interface to simulated G4-A only.

The operating system must actually isolate workers from the writer UID and
its files. This interface does not protect against code running as the writer,
root, inherited file descriptors, capabilities, or another host. It provides no
provider dispatch authority and is not a production deployment mechanism.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import struct
import sys

from .experimental_g4a_settlement import ExperimentalG4AAuthority, SimulatedEvidence
from .registry import OperationRecord
from .sqlite_backend import SQLiteConsensusBackend

MAX_REQUEST = 65536
_MAX_RESPONSE = 131072


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _checked_directory(path, uid, *, private):
    path = Path(path)
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise RuntimeError("absolute normalized directory required")
    # lstat every ancestor: no symlinks and no untrusted writable parent.
    for current in reversed((path, *path.parents)):
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
            raise RuntimeError("untrusted directory owner or type")
        if info.st_mode & 0o022:
            raise RuntimeError("writable directory in authority path")
    info = path.lstat()
    if info.st_uid != uid or (private and info.st_mode & 0o077):
        raise RuntimeError("authority directory is not private to writer")
    return path


def _read_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ValueError("truncated request")
        data.extend(chunk)
    return bytes(data)


class ExperimentalUidG4AService:
    """A bounded IPC endpoint for an already-provisioned simulated store."""

    def __init__(self, state_dir, socket_dir, *, worker_uids, trust_generation):
        if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Linux peer credentials required")
        uid = os.geteuid()
        if uid == 0 or not worker_uids or any(type(u) is not int or u <= 0 or u == uid for u in worker_uids):
            raise RuntimeError("distinct non-root writer and worker UIDs required")
        self.state_dir = _checked_directory(state_dir, uid, private=True)
        self.socket_dir = _checked_directory(socket_dir, uid, private=False)
        if self.socket_dir == self.state_dir or self.state_dir in self.socket_dir.parents:
            raise RuntimeError("socket directory must be outside private state")
        # Check existing content before SQLite or the registry opens anything.
        for root, dirs, files in os.walk(self.state_dir, followlinks=False):
            for name in dirs + files:
                info = (Path(root) / name).lstat()
                if stat.S_ISLNK(info.st_mode) or info.st_uid != uid:
                    raise RuntimeError("untrusted state entry")
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise RuntimeError("unsupported state entry")
        budget = self.state_dir / "budget"
        _checked_directory(budget, uid, private=True)
        if not (budget / "consensus.sqlite3").is_file():
            raise RuntimeError("preprovisioned store required")
        # Inspect the stored source before constructing a backend, which may
        # otherwise open or create files outside the protected directory.
        with sqlite3.connect((budget / "consensus.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT source_path FROM experimental_g3_exclusive_epoch WHERE singleton=1").fetchone()
        if not row or not isinstance(row[0], str):
            raise RuntimeError("registry source is missing")
        source = Path(row[0])
        if not source.is_absolute() or not source.is_relative_to(self.state_dir) or source.resolve() != source:
            raise RuntimeError("registry source is outside protected state")
        self.allowed = frozenset(worker_uids)
        self.socket_path = self.socket_dir / "g4a-lab.sock"
        self.authority = ExperimentalG4AAuthority.open(
            SQLiteConsensusBackend(str(budget)), trust_generation=trust_generation,
            experimental_only=True,
        )
        # G3 stores its source path in SQLite; it must remain within private state.
        if Path(self.authority.registry._store_path) != source:
            raise RuntimeError("registry source is outside protected state")
        self.listener = None

    def open(self):
        if os.path.lexists(self.socket_path):
            raise RuntimeError("preexisting socket path refused")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o666)
            listener.listen(16)
        except BaseException:
            listener.close()
            raise
        self.listener = listener

    def close(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            # The non-writable socket directory is owned by the writer.
            os.unlink(self.socket_path)

    def _dispatch(self, obj):
        if not isinstance(obj, dict) or obj.get("schema") != "tenir.g4a.uid-lab.v1":
            raise ValueError("invalid request")
        action = obj.get("action")
        if action == "inspect" and set(obj) == {"schema", "action", "operation_key"}:
            return asdict(self.authority.inspect(obj["operation_key"]))
        if action == "admit" and set(obj) == {"schema", "action", "record", "attempt_id", "request_id"}:
            record = OperationRecord.from_dict(obj["record"])
            return asdict(self.authority.admit_created(record, attempt_id=obj["attempt_id"], request_id=obj["request_id"]))
        if action == "settle" and set(obj) == {"schema", "action", "request_id", "evidence"}:
            if not isinstance(obj["evidence"], list) or len(obj["evidence"]) > 16:
                raise ValueError("invalid evidence")
            return asdict(self.authority.settle(obj["request_id"], [SimulatedEvidence(**e) for e in obj["evidence"]]))
        raise ValueError("unsupported request")

    def serve_one(self):
        if self.listener is None:
            raise RuntimeError("service is not listening")
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(3)
            try:
                _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in self.allowed:
                    raise PermissionError("peer UID refused")
                length = struct.unpack("!I", _read_exact(connection, 4))[0]
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError("request size refused")
                obj = json.loads(_read_exact(connection, length), object_pairs_hook=_unique_object,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
                result = self._dispatch(obj)
                response = {"ok": True, "receipt": result}
            except Exception:
                response = {"ok": False, "error": "request refused"}
            data = json.dumps(response, allow_nan=False, separators=(",", ":")).encode()
            if len(data) > _MAX_RESPONSE:
                data = b'{"ok":false,"error":"response too large"}'
            try:
                connection.sendall(struct.pack("!I", len(data)) + data)
            except OSError:
                pass  # durable transaction outcome must be recovered by exact replay
