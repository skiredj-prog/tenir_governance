"""G1 OS isolation — RED-first integration contract, not a production assurance test.

Run in a disposable POSIX sandbox. Baseline G4-A does NOT implement an isolated
storage service, so these tests SKIP until an adapter is provided. Do not turn
skips into passes or infer OS isolation from SQLite triggers.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX isolation contract")


@pytest.fixture
def isolation_adapter():
    """External fixture must provision distinct UIDs and an isolated store.

    Required API:
      .db_path: Path
      .sidecar_paths: iterable[Path]
      .agent_uid: int
      .storage_uid: int
      .invoke_as_agent(argv: list[str]) -> subprocess.CompletedProcess
      .authorized_mutation(operation_key: str) -> object
      .read_state() -> bytes
      .grant(operation_key: str, epoch: int) -> object
      .submit_grant(grant: object, operation_key: str, epoch: int) -> object
      .revoke_epoch() -> None
    Never use chmod to grant the attacker write access during a conformant test.
    """
    pytest.skip("G1 isolation adapter not implemented; no security verdict")


def test_g1_01_agent_cannot_open_sqlite_directly(isolation_adapter):
    a = isolation_adapter
    assert a.agent_uid != a.storage_uid
    before = a.read_state()
    script = (
        "import sqlite3,sys; "
        "c=sqlite3.connect(sys.argv[1]); "
        "c.execute('CREATE TABLE IF NOT EXISTS g1_bypass(x INTEGER)'); c.commit()"
    )
    result = a.invoke_as_agent([sys.executable, "-c", script, str(a.db_path)])
    assert result.returncode != 0, "FAIL: uninstrumented direct SQL committed"
    assert a.read_state() == before


def test_g1_02_agent_cannot_replace_database_or_sidecars(isolation_adapter):
    a = isolation_adapter
    before = a.read_state()
    for path in [a.db_path, *a.sidecar_paths]:
        script = (
            "import os,sys,pathlib; p=pathlib.Path(sys.argv[1]); "
            "tmp=p.with_name(p.name+'.g1-attack'); tmp.write_bytes(b'corrupt'); "
            "os.replace(tmp,p)"
        )
        result = a.invoke_as_agent([sys.executable, "-c", script, str(path)])
        assert result.returncode != 0, f"FAIL: agent replaced {path}"
    assert a.read_state() == before


def test_g1_03_grant_not_transferable_between_operation_or_epoch(isolation_adapter):
    a = isolation_adapter
    grant = a.grant("lab:op-a", 7)
    assert a.submit_grant(grant, "lab:op-b", 7).denied
    assert a.submit_grant(grant, "lab:op-a", 8).denied


def test_g1_04_revoked_epoch_rejected_before_mutation(isolation_adapter):
    a = isolation_adapter
    grant = a.grant("lab:op-a", 7)
    a.revoke_epoch()
    before = a.read_state()
    assert a.submit_grant(grant, "lab:op-a", 7).denied
    assert a.read_state() == before


def test_g1_05_authorized_pipeline_control(isolation_adapter):
    a = isolation_adapter
    before = a.read_state()
    receipt = a.authorized_mutation("lab:authorized")
    assert receipt is not None
    assert a.read_state() != before


def test_g1_06_same_uid_privileged_writer_is_negative_control(isolation_adapter):
    """Require an explicit separate threat-model experiment for same-UID writer.

    This test must not silently label a privileged SQLite bypass as G1 PASS.
    """
    pytest.xfail("Same-UID privileged attacker requires dedicated negative control and verdict")
