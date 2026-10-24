"""Deployment manifest schema and artifact verification tests."""

import hashlib
import json

import pytest

import tenir_governance
import tenir_kernel
from tenir_kernel.deployment_manifest import verify_deployment_manifest
from tenir_governance.deployment_safety import (
    MULTI_HOST_DISABLED_MESSAGE,
    RAFT_PRODUCTION_DISABLED_MESSAGE,
    enforce_host_scope,
    require_production_manifest,
)


def _write_valid_manifest(directory):
    artifact = directory / "service.bin"
    artifact.write_bytes(b"reviewed deployment artifact")
    manifest = {
        "manifest_version": "1",
        "deployment_id": "test-deployment",
        "release": {"package": "tenir-governance", "version": "5.7.0"},
        "runtime_contract": {"engine_spec_version": "TENIR-2.0", "schema_version": "1.2"},
        "deployment": {"environment": "test", "host_scope": "single-host"},
        "mode": "ENFORCE",
        "governance_controls": {"i2_payload_binding": True, "i5_conflict_hold": True},
        "budget_store": {"backend": "sqlite_wal", "database": "state/consensus.sqlite3"},
        "raft": {
            "enabled": False,
            "peer_authentication": False,
            "serialization": "not_used",
            "production_allowed": False,
        },
        "artifacts": [{
            "path": "service.bin",
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }],
    }
    path = directory / "deployment.manifest"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def test_verifier_accepts_manifest_and_checks_artifact_digest(tmp_path):
    path, _manifest = _write_valid_manifest(tmp_path)

    result = verify_deployment_manifest(path)

    assert result["valid"] is True
    assert result["release_version"] == "5.7.0"
    assert result["artifact_count"] == 1


def test_verifier_rejects_artifact_digest_mismatch(tmp_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["artifacts"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_deployment_manifest(path)


@pytest.mark.parametrize("raft", [
    {"enabled": True, "peer_authentication": False, "serialization": "pickle", "production_allowed": False},
    {"enabled": True, "peer_authentication": True, "serialization": "json", "production_allowed": False},
    {"enabled": True, "peer_authentication": True, "serialization": "pickle", "production_allowed": True},
])
def test_verifier_rejects_unsecured_raft_manifest(tmp_path, raft):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["raft"] = raft
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid deployment manifest"):
        verify_deployment_manifest(path)


def test_verifier_rejects_missing_required_governance_controls(tmp_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["governance_controls"]["i2_payload_binding"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid deployment manifest"):
        verify_deployment_manifest(path)


def test_verifier_rejects_v57_multi_host_manifest(tmp_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["deployment"]["host_scope"] = "multi-host"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Multi-host is disabled in v5.7.0"):
        verify_deployment_manifest(path)


def test_verifier_keeps_fm20_open_for_v57_production_raft(tmp_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["deployment"]["environment"] = "production"
    manifest["raft"] = {
        "enabled": True,
        "peer_authentication": True,
        "serialization": "pickle",
        "production_allowed": False,
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="FM-20 is open"):
        verify_deployment_manifest(path)


def test_host_scope_guard_is_explicit_and_version_bounded():
    with pytest.raises(RuntimeError, match="Multi-host is disabled in v5.7.0"):
        enforce_host_scope({"HOST_SCOPE": "multi-host"}, release_version="5.7.0")

    assert enforce_host_scope({"HOST_SCOPE": "multi-host"}, release_version="5.8.0") == "multi-host"


def test_v57_rejects_multi_host_scope_alias():
    with pytest.raises(RuntimeError, match="Multi-host is disabled in v5.7.0"):
        enforce_host_scope({"TENIR_HOST_SCOPE": "multi-host"}, release_version="5.7.0")


def test_conflicting_host_scope_aliases_fail_closed():
    with pytest.raises(RuntimeError, match="HOST_SCOPE and TENIR_HOST_SCOPE conflict"):
        enforce_host_scope({
            "HOST_SCOPE": "single-host",
            "TENIR_HOST_SCOPE": "multi-host",
        }, release_version="5.7.0")


def test_conflicting_environment_aliases_fail_closed():
    from tenir_governance.deployment_safety import runtime_environment

    with pytest.raises(RuntimeError, match="TENIR_ENV and ENVIRONMENT conflict"):
        runtime_environment({"TENIR_ENV": "development", "ENVIRONMENT": "production"})


def test_kernel_and_governance_release_versions_match():
    assert tenir_kernel.__version__ == tenir_governance.__version__ == "5.7.0"


def test_api_startup_refuses_multi_host_scope(monkeypatch):
    from tenir_kernel.api import create_app

    monkeypatch.setenv("HOST_SCOPE", "multi-host")
    monkeypatch.delenv("TENIR_DEPLOYMENT_MANIFEST", raising=False)

    with pytest.raises(RuntimeError, match="Multi-host is disabled in v5.7.0"):
        create_app()


def test_cli_serve_refuses_multi_host_scope(monkeypatch):
    from tenir_kernel.cli import main

    monkeypatch.setenv("HOST_SCOPE", "multi-host")
    monkeypatch.delenv("TENIR_DEPLOYMENT_MANIFEST", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main(["serve"])

    assert exc_info.value.code == 2


def test_api_production_startup_requires_manifest(monkeypatch):
    from tenir_kernel.api import create_app

    monkeypatch.setenv("TENIR_ENV", "production")
    monkeypatch.setenv("HOST_SCOPE", "single-host")
    monkeypatch.delenv("TENIR_DEPLOYMENT_MANIFEST", raising=False)

    with pytest.raises(RuntimeError, match="TENIR_DEPLOYMENT_MANIFEST is required"):
        create_app()


def test_raft_backend_refuses_production_startup(monkeypatch):
    from tenir_governance.operations.raft_backend import RaftConsensusBackend

    monkeypatch.setenv("TENIR_ENV", "production")
    monkeypatch.setenv("HOST_SCOPE", "single-host")

    with pytest.raises(RuntimeError, match="FM-20 is open"):
        RaftConsensusBackend("127.0.0.1:7000", [], peer_auth_key="x" * 32)


def test_production_startup_requires_a_matching_manifest(tmp_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["deployment"]["environment"] = "production"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    environ = {
        "TENIR_ENV": "production",
        "HOST_SCOPE": "single-host",
        "TENIR_DEPLOYMENT_MANIFEST": str(path),
    }

    verified = require_production_manifest(environ, verify_deployment_manifest)

    assert verified["environment"] == "production"
    assert verified["host_scope"] == "single-host"
    assert verified["raft_enabled"] is False
    assert verified["raft_production_allowed"] is False


def test_production_startup_fails_closed_without_manifest():
    with pytest.raises(RuntimeError, match="TENIR_DEPLOYMENT_MANIFEST is required"):
        require_production_manifest({"TENIR_ENV": "production"}, verify_deployment_manifest)


def test_production_startup_rejects_manifest_for_another_release(tmp_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["deployment"]["environment"] = "production"
    manifest["release"]["version"] = "5.7.1"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    environ = {
        "TENIR_ENV": "production",
        "HOST_SCOPE": "single-host",
        "TENIR_DEPLOYMENT_MANIFEST": str(path),
    }

    with pytest.raises(RuntimeError, match="does not match installed package"):
        require_production_manifest(environ, verify_deployment_manifest)


def test_raft_error_remains_specific_to_fm20():
    assert "FM-20" in MULTI_HOST_DISABLED_MESSAGE
    assert "FM-20" in RAFT_PRODUCTION_DISABLED_MESSAGE


@pytest.mark.parametrize("relative_path", ["../outside.bin", "nested/../../outside.bin"])
def test_verifier_rejects_artifact_path_escape(tmp_path, relative_path):
    path, manifest = _write_valid_manifest(tmp_path)
    manifest["artifacts"][0]["path"] = relative_path
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="must stay within"):
        verify_deployment_manifest(path)
