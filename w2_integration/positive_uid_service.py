"""Linux-only, opt-in UID boundary for the *synthetic* positive laboratory.

This does not connect a real provider, establish production custody, isolate a
host administrator or defend against code running as the writer UID. The host
must install trusted source and provision distinct users and protected paths.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import sys

from kernel_claim_provider import KernelClaimProofProvider
from native_positive_g4a import NativePositiveG4A
from native_positive_permit import NativePositivePermitAuthority
from tenir_governance.operations.experimental_g4a_uid_service import (
    ExperimentalUidG4AService, _checked_directory,
)
from tenir_governance.operations.sqlite_backend import SQLiteConsensusBackend
from w2_2b_canonical import request_hash
from w2_integration.native_dispatch_command import snapshot_command


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("invalid identifier")
    return value


class ExperimentalPositiveUidService(ExperimentalUidG4AService):
    """Exposes only native positive laboratory operations to selected UIDs."""

    def __init__(self, state_dir, socket_dir, *, worker_uids):
        if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Linux peer credentials required")
        uid = os.geteuid()
        if uid == 0 or not worker_uids or any(type(x) is not int or x <= 0 or x == uid for x in worker_uids):
            raise RuntimeError("distinct non-root writer and worker UIDs required")
        old_mask = os.umask(0o077)
        os.umask(old_mask)
        if old_mask & 0o077 != 0o077:
            raise RuntimeError("writer must start with private umask")
        self.state_dir = _checked_directory(state_dir, uid, private=True)
        self.socket_dir = _checked_directory(socket_dir, uid, private=False)
        if self.socket_dir == self.state_dir or self.state_dir in self.socket_dir.parents:
            raise RuntimeError("socket directory must be outside private state")
        for root, dirs, files in os.walk(self.state_dir, followlinks=False):
            for name in dirs + files:
                info = (Path(root) / name).lstat()
                if (stat.S_ISLNK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077
                        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                    raise RuntimeError("untrusted or nonprivate state entry")
        for name in ("TENIR_W2_PRIVATE_KEY_HEX", "TENIR_WITNESS_PRIVATE_KEY_HEX", "TENIR_B3_PRIVATE_KEY_HEX"):
            if not os.environ.get(name):
                raise RuntimeError("injected laboratory key required")
        budget = self.state_dir / "budget"
        _checked_directory(budget, uid, private=True)
        if not (budget / "consensus.sqlite3").is_file():
            raise RuntimeError("preprovisioned store required")
        self.allowed = frozenset(worker_uids)
        self.socket_path = self.socket_dir / "positive-lab.sock"
        self.authority = NativePositivePermitAuthority(SQLiteConsensusBackend(str(budget)))
        self.gate = NativePositiveG4A(self.authority)
        self.provider = KernelClaimProofProvider(
            db_path=str(self.state_dir / "synthetic-provider.sqlite3"),
            claim_verifier=self.authority.verify_dispatch_claim,
        )
        self.listener = None

    def close(self):
        try:
            super().close()
        finally:
            self.provider.conn.close()

    def _dispatch(self, obj):
        if not isinstance(obj, dict) or obj.get("schema") != "tenir.positive.uid-lab.v1":
            raise ValueError("invalid request")
        if obj.get("action") != "submit" or set(obj) != {
            "schema", "action", "operation_key", "evaluation_id", "idempotency_key", "command"
        }:
            raise ValueError("unsupported request")
        operation = _identifier(obj["operation_key"])
        evaluation = _identifier(obj["evaluation_id"])
        idempotency = _identifier(obj["idempotency_key"])
        command = snapshot_command(obj["command"])
        with self.authority._tx() as db:
            prior = db.execute(
                "SELECT operation_key, idempotency_key, request_hash, status FROM w2_positive_v1_outbox "
                "WHERE operation_key=? OR idempotency_key=?",
                (operation, idempotency),
            ).fetchone()
        if prior and (prior["operation_key"] != operation or prior["idempotency_key"] != idempotency
                      or prior["request_hash"] != request_hash(command)):
            raise ValueError("operation identity conflict")
        grant = self.gate.evaluate_and_grant(operation, evaluation, command, resource_id=command["resource_id"])
        if prior and prior["status"] != "PENDING":
            return {"status": prior["status"], "synthetic": True, "execution_authorized": False}
        result = self.gate.dispatch_to_b3(operation, idempotency, command, grant, self.provider)
        return {"status": result["status"], "synthetic": True, "execution_authorized": False}
