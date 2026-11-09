"""Opt-in Governor client for the synthetic, approved Linux UID laboratory.

The only supported remote endpoint performs synthetic local effects. This
provider never reports external success or failure, and is not a production
connector. Local Governor and remote native budgets remain separate stores.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys

from tenir_governance.operations.experimental_g4a_uid_service import (
    MAX_REQUEST, _MAX_RESPONSE, _checked_directory, _read_exact, _unique_object,
)
from tenir_governance.operations.provider import DispatchResult, ProviderOutcome, ReconciliationResult
from tenir_governance.operations.registry import OperationState
from w2_integration.native_dispatch_command import snapshot_command, snapshot_payload


class ExperimentalGovernorUidProvider:
    """Fail-closed proxy for a signed synthetic request, not real dispatch."""

    def __init__(self, socket_path, *, writer_uid, registry):
        if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Linux peer credentials required")
        if type(writer_uid) is not int or writer_uid <= 0 or writer_uid == os.geteuid() or os.geteuid() == 0:
            raise RuntimeError("distinct non-root writer and worker UIDs required")
        path = Path(socket_path)
        if not path.is_absolute() or str(path) != os.path.normpath(str(path)) or path.name != "positive-lab.sock":
            raise RuntimeError("normalized positive laboratory socket path required")
        _checked_directory(path.parent, writer_uid, private=False)
        self.socket_path = path
        self.writer_uid = writer_uid
        if not callable(getattr(registry, "get", None)):
            raise RuntimeError("Governor registry required")
        self.registry = registry

    def _request(self, idempotency_key, action_type, amount, agent_id, metadata):
        envelope = snapshot_payload(metadata)
        if set(envelope) != {"b1_lab_request"}:
            raise RuntimeError("signed laboratory request envelope required")
        request = envelope["b1_lab_request"]
        if not isinstance(request, dict) or set(request) != {
                "schema", "action", "operation_key", "evaluation_id", "idempotency_key", "command", "approval"}:
            raise RuntimeError("signed laboratory request required")
        if request["schema"] != "tenir.positive.approved.uid-lab.v1" or request["action"] != "submit":
            raise RuntimeError("unsupported laboratory request")
        command = snapshot_command(request["command"])
        if (not isinstance(idempotency_key, str) or not idempotency_key
                or request["operation_key"] != idempotency_key or request["idempotency_key"] != idempotency_key
                or command["action_type"] != action_type or command["agent_id"] != agent_id):
            raise RuntimeError("Governor and signed operation identity differ")
        try:
            if (type(amount) not in (int, float) or not Decimal(str(amount)).is_finite()
                    or Decimal(str(amount)) != Decimal(str(command["amount"]))):
                raise RuntimeError("Governor and signed amount differ")
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError("Governor amount invalid") from exc
        record = self.registry.get(idempotency_key)
        if (record is None or record.state != OperationState.DISPATCHING
                or record.idempotency_key != idempotency_key
                or record.resource_id != command["resource_id"]
                or record.action_type != command["action_type"] or record.agent_id != command["agent_id"]
                or Decimal(str(record.amount)) != Decimal(str(command["amount"]))):
            raise RuntimeError("Governor registry and signed command differ")
        request["command"] = command
        return request

    def _exchange(self, request):
        # The parent is protected against untrusted writes. Recheck the socket
        # path and authenticate the connected server before sending any data.
        _checked_directory(self.socket_path.parent, self.writer_uid, private=False)
        info = self.socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.writer_uid:
            raise RuntimeError("untrusted laboratory socket")
        data = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if not 0 < len(data) <= MAX_REQUEST:
            raise RuntimeError("laboratory request too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(str(self.socket_path))
            _, peer_uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer_uid != self.writer_uid:
                raise RuntimeError("laboratory server UID refused")
            conn.sendall(struct.pack("!I", len(data)) + data)
            size = struct.unpack("!I", _read_exact(conn, 4))[0]
            if not 0 < size <= _MAX_RESPONSE:
                raise RuntimeError("laboratory response too large")
            return json.loads(_read_exact(conn, size), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))

    def execute(self, idempotency_key, action_type, amount, agent_id, metadata=None):
        try:
            request = self._request(idempotency_key, action_type, amount, agent_id, metadata)
            response = self._exchange(request)
            receipt = response.get("receipt") if isinstance(response, dict) and response.get("ok") is True else None
            if (not isinstance(receipt, dict) or set(receipt) != {"status", "synthetic", "execution_authorized"}
                    or receipt["synthetic"] is not True or receipt["execution_authorized"] is not False
                    or receipt["status"] not in ("EXECUTED", "UNKNOWN")):
                raise RuntimeError("laboratory request refused or invalid response")
        except Exception:
            return DispatchResult(outcome=ProviderOutcome.UNKNOWN,
                                  error="B1 laboratory request refused or result unavailable")
        # Synthetic ledger records do not establish a real external effect.
        return DispatchResult(outcome=ProviderOutcome.UNKNOWN,
                              raw_response={"synthetic": True, "laboratory_status": receipt["status"]},
                              error="Synthetic laboratory outcome; external effect not established")

    def reconcile(self, idempotency_key, provider_reference=""):
        return ReconciliationResult(resolved=False, outcome=ProviderOutcome.UNKNOWN,
                                    error="Laboratory proxy cannot establish an external effect")
