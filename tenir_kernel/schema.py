"""JSON Schema 1.2 validation helpers (optional dependency: jsonschema)."""

from __future__ import annotations

from importlib.resources import files
import json
from typing import Any, Mapping


def load_runtime_schema() -> dict[str, Any]:
    path = files("tenir_kernel").joinpath("schemas/tenir_1_2.schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_runtime_object(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("TENIR runtime object must be a JSON object")
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "TENIR schema validation requires the API extra: pip install -e '.[api]'"
        ) from exc

    schema = load_runtime_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(dict(value)), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
            for error in errors[:8]
        )
        raise ValueError(f"invalid TENIR schema 1.2 runtime object: {details}")
    return dict(value)
