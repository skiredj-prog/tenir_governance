"""Canonical JSON payload hashing for authorization-to-dispatch binding."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


EMPTY_PAYLOAD_HASH = hashlib.sha256(b"{}").hexdigest()


def _validate_json_value(value: Any, path: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a value outside the JSON data model")


def canonical_payload_hash(payload: Mapping[str, Any] | None) -> str:
    """Hash a JSON object with strict, deterministic serialization.

    Object key order and insignificant whitespace do not affect the digest.
    Unsupported Python objects, non-string keys, and NaN/Infinity are rejected
    so different runtimes cannot authorize different meanings for one hash.
    """
    if payload is None:
        normalized: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        normalized = dict(payload)
    else:
        raise ValueError("payload must be a JSON object")
    _validate_json_value(normalized)
    try:
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("payload cannot be encoded as canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()
