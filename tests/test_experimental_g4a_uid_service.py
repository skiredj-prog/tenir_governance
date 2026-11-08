"""Local protocol checks; real UID separation requires the Linux campaign."""
from dataclasses import dataclass
import sys

import pytest

from tenir_governance.operations.experimental_g4a_uid_service import (
    ExperimentalUidG4AService, _unique_object,
)


@dataclass
class Receipt:
    decision: str = "HOLD"
    execution_authorized: bool = False


class FakeAuthority:
    def __init__(self):
        self.calls = []

    def inspect(self, key):
        self.calls.append(key)
        return Receipt()


def test_unsupported_platform_refuses_before_opening_store(tmp_path):
    if sys.platform == "linux":
        pytest.skip("non-Linux refusal tested on other platforms")
    with pytest.raises(RuntimeError, match="Linux"):
        ExperimentalUidG4AService(tmp_path, tmp_path, worker_uids=[123], trust_generation=1)


def test_duplicate_json_member_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        _unique_object([("action", "inspect"), ("action", "settle")])


def test_protocol_exposes_only_explicit_commands():
    service = object.__new__(ExperimentalUidG4AService)
    service.authority = FakeAuthority()
    request = {"schema": "tenir.g4a.uid-lab.v1", "action": "inspect", "operation_key": "lab:1"}
    assert service._dispatch(request) == {"decision": "HOLD", "execution_authorized": False}
    assert service.authority.calls == ["lab:1"]
    for bad in ({**request, "sql": "DROP TABLE resources"},
                {**request, "action": "execute"},
                {**request, "schema": "other"}):
        with pytest.raises(ValueError):
            service._dispatch(bad)
    assert service.authority.calls == ["lab:1"]
