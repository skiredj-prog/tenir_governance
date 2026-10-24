"""Fail-closed deployment policy for the v5.7 release line."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping


MULTI_HOST_DISABLED_MESSAGE = (
    "Multi-host is disabled in v5.7.0 due to open FM-20 "
    "(PySyncObj transport). See Threat Model."
)
RAFT_PRODUCTION_DISABLED_MESSAGE = (
    "Raft is not allowed in production while FM-20 is open "
    "(PySyncObj command transport uses pickle). See Threat Model."
)
_ENVIRONMENTS = {"development", "test", "staging", "production"}
_HOST_SCOPES = {"single-host", "multi-host"}


def current_release_version() -> str:
    """Return the source package version, falling back closed to this candidate."""
    try:
        from tenir_governance import __version__

        return str(__version__)
    except (ImportError, AttributeError):
        try:
            return version("tenir-governance")
        except PackageNotFoundError:
            return "5.7.0"


def runtime_environment(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    primary = values.get("TENIR_ENV")
    alias = values.get("ENVIRONMENT")
    if primary is not None and alias is not None:
        if primary.strip().lower() != alias.strip().lower():
            raise RuntimeError("TENIR_ENV and ENVIRONMENT conflict")
    environment = (primary if primary is not None else alias or "development")
    environment = environment.strip().lower()
    if environment not in _ENVIRONMENTS:
        raise RuntimeError(
            "TENIR_ENV must be development, test, staging, or production"
        )
    return environment


def enforce_host_scope(
    environ: Mapping[str, str] | None = None,
    *,
    release_version: str | None = None,
) -> str:
    """Reject unsupported host scopes before an API or consensus node starts."""
    values = os.environ if environ is None else environ
    primary = values.get("HOST_SCOPE")
    alias = values.get("TENIR_HOST_SCOPE")
    if primary is not None and alias is not None:
        normalized_primary = primary.strip().lower().replace("_", "-")
        normalized_alias = alias.strip().lower().replace("_", "-")
        if normalized_primary != normalized_alias:
            raise RuntimeError("HOST_SCOPE and TENIR_HOST_SCOPE conflict")
    scope = (primary if primary is not None else alias or "single-host")
    scope = scope.strip().lower().replace("_", "-")
    if scope not in _HOST_SCOPES:
        raise RuntimeError("HOST_SCOPE must be single-host or multi-host")

    running_version = release_version or current_release_version()
    if running_version.startswith("5.7.") and scope == "multi-host":
        raise RuntimeError(MULTI_HOST_DISABLED_MESSAGE)
    return scope


def require_production_manifest(
    environ: Mapping[str, str] | None,
    verify_manifest,
) -> dict | None:
    """Require and cross-check the manifest whenever production mode is selected."""
    values = os.environ if environ is None else environ
    environment = runtime_environment(values)
    scope = enforce_host_scope(values)
    manifest_path = values.get("TENIR_DEPLOYMENT_MANIFEST")

    if environment == "production" and not manifest_path:
        raise RuntimeError(
            "TENIR_DEPLOYMENT_MANIFEST is required when TENIR_ENV=production"
        )
    if not manifest_path:
        return None

    result = verify_manifest(manifest_path)
    if result.get("release_version") != current_release_version():
        raise RuntimeError("deployment manifest release version does not match installed package")
    if result.get("environment") != environment:
        raise RuntimeError("deployment manifest environment does not match TENIR_ENV")
    if result.get("host_scope") != scope:
        raise RuntimeError("deployment manifest host_scope does not match HOST_SCOPE")
    if environment == "production":
        if result.get("raft_enabled") is not False:
            raise RuntimeError(RAFT_PRODUCTION_DISABLED_MESSAGE)
        if result.get("raft_production_allowed") is not False:
            raise RuntimeError(RAFT_PRODUCTION_DISABLED_MESSAGE)
    return result
