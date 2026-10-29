"""Versioned command binding for the experimental DispatchProvider adapter.

This schema binds the arguments passed to execute(). Connector-side defaults
and the external interpretation of metadata remain separate trust inputs.
"""
from __future__ import annotations

import json
import math


COMMAND_SCHEMA = "tenir-dispatch-command/v1"
_FIELDS = {"schema", "resource_id", "amount", "action_type", "agent_id", "metadata"}


def _json_value(value):
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise RuntimeError("command JSON keys must be strings")
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif type(value) is float:
        if not math.isfinite(value):
            raise RuntimeError("command JSON numbers must be finite")
    elif type(value) not in (str, int, bool, type(None)):
        raise RuntimeError("command must contain only JSON values")


def snapshot_payload(payload: dict) -> dict:
    """Detach every approval payload before reading schema, amount or identity."""
    if type(payload) is not dict:
        raise RuntimeError("dispatch payload must be a JSON object")
    _json_value(payload)
    try:
        command = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RuntimeError) as exc:
        raise RuntimeError("command could not be snapshotted") from exc
    return command


def snapshot_command(payload: dict) -> dict:
    """Detach and validate a complete command without inventing approval fields."""
    command = snapshot_payload(payload)
    if set(command) != _FIELDS:
        missing, extra = sorted(_FIELDS - set(command)), sorted(set(command) - _FIELDS)
        raise RuntimeError(f"command fields invalid; missing={missing}, extra={extra}")
    if command["schema"] != COMMAND_SCHEMA:
        raise RuntimeError("unsupported dispatch command schema")
    for name in ("resource_id", "action_type", "agent_id"):
        value = command[name]
        if type(value) is not str or not value.strip() or len(value) > 256:
            raise RuntimeError(f"command {name} must be explicit nonempty text")
    amount = command["amount"]
    if (type(amount) not in (int, float) or amount <= 0
            or (type(amount) is float and not math.isfinite(amount))):
        raise RuntimeError("command amount must be a positive finite JSON number")
    if type(command["metadata"]) is not dict:
        raise RuntimeError("command metadata must be an explicit JSON object")
    return command
