"""Schema and artifact-integrity verification for deployment.manifest."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any


def load_deployment_manifest_schema() -> dict[str, Any]:
    source = files("tenir_kernel").joinpath("schemas/deployment.manifest")
    return json.loads(source.read_text(encoding="utf-8"))


def verify_deployment_manifest(path: str | Path) -> dict[str, Any]:
    """Validate manifest structure and every relative artifact SHA-256 digest."""
    try:
        manifest_path = Path(path).resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        from jsonschema import Draft202012Validator
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read deployment manifest: {exc}") from exc
    except ImportError as exc:
        raise RuntimeError("Manifest validation requires jsonschema, included in the base package.") from exc

    errors = sorted(
        Draft202012Validator(load_deployment_manifest_schema()).iter_errors(manifest),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        detail = "; ".join(
            f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
            for error in errors[:8]
        )
        raise ValueError(f"invalid deployment manifest: {detail}")

    release_version = manifest["release"]["version"]
    environment = manifest["deployment"]["environment"]
    host_scope = manifest["deployment"]["host_scope"]
    raft = manifest["raft"]
    if release_version.startswith("5.7.") and host_scope == "multi-host":
        from tenir_governance.deployment_safety import MULTI_HOST_DISABLED_MESSAGE

        raise ValueError(MULTI_HOST_DISABLED_MESSAGE)
    if release_version.startswith("5.7.") and environment == "production" and raft["enabled"]:
        from tenir_governance.deployment_safety import RAFT_PRODUCTION_DISABLED_MESSAGE

        raise ValueError(RAFT_PRODUCTION_DISABLED_MESSAGE)

    root = manifest_path.parent.resolve()
    verified = []
    for artifact in manifest["artifacts"]:
        relative = Path(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"artifact path must stay within the manifest directory: {relative}")
        artifact_path = (root / relative).resolve(strict=True)
        try:
            artifact_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"artifact escapes the manifest directory: {relative}") from exc
        if not artifact_path.is_file():
            raise ValueError(f"artifact is not a regular file: {relative}")
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if digest != artifact["sha256"]:
            raise ValueError(f"SHA-256 mismatch for deployment artifact: {relative}")
        verified.append({"path": relative.as_posix(), "sha256": digest})

    return {
        "valid": True,
        "manifest_version": manifest["manifest_version"],
        "deployment_id": manifest["deployment_id"],
        "release_version": manifest["release"]["version"],
        "environment": environment,
        "host_scope": host_scope,
        "raft_enabled": raft["enabled"],
        "raft_production_allowed": raft["production_allowed"],
        "artifact_count": len(verified),
        "artifacts": verified,
    }
