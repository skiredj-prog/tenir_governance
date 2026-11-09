"""Scoped W2 entrypoint refusals, externally observed and mutation-sensitive.

W2_TEST_MUTANT is read ONLY in this test module. The separate mutation campaign
must fail these same assertions; it is never an application configuration.
"""
import ast
from contextlib import ExitStack, closing
from copy import deepcopy
import inspect
import os
from pathlib import Path
import sqlite3
import textwrap

import httpx
import pytest

import lab_s1_bridge
from i8_ledger import I8Ledger
from lab_s1_bridge import RC01Lab
from native_positive_g4a import NativePositiveG4A
from proof_provider import ProofProvider
from test_b1_effect_contracts import system, approved
from w2_2_positive_g4a import PositiveG4A
from w2_regression_support import RESOURCE, command, native_lab


MUTANT = os.environ.get("W2_TEST_MUTANT") == "1"


def _assert_refused(refused, requests, before, after):
    # Check the independent transport observer BEFORE the local ledger.
    assert requests == [], "provider transport was called"
    assert after == before, "denied request changed I8 or pending-send state"
    assert refused, "entrypoint did not refuse unauthenticated execution"


def _state(backend):
    with backend._connection() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for table in ("resources", "reservations", "w2_positive_v1_capacity",
                      "w2_positive_v1_outbox", "b1_effect_commands"):
            if table in tables:
                result[table] = [tuple(r) for r in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
        return result


@pytest.fixture
def observed_b3(monkeypatch):
    requests = []
    def record(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"synthetic": True})
    with httpx.Client(transport=httpx.MockTransport(record), trust_env=False) as client:
        original = ProofProvider._sign
        def observe_effect(provider, body):
            # B3's synthetic effect boundary, after authentication and before
            # receipt storage. Observer is independent of every TENIR database.
            client.post("https://provider.invalid/mock-b3-effect", json={"operation_id": body["operation_id"]})
            return original(provider, body)
        monkeypatch.setattr(ProofProvider, "_sign", observe_effect)
        yield requests


def _remove_fail_closed_guard(owner, name, monkeypatch):
    function = getattr(owner, name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    body = tree.body[0].body
    guards = [node for node in body if isinstance(node, ast.Raise)
              and isinstance(node.exc, ast.Call) and node.exc.args
              and isinstance(node.exc.args[0], ast.Constant)
              and "legacy" in str(node.exc.args[0].value)]
    assert len(guards) == 1, "mutation must remove exactly the legacy refusal"
    body.remove(guards[0])
    namespace = dict(function.__globals__)
    exec(compile(ast.fix_missing_locations(tree), function.__code__.co_filename, "exec"), namespace)
    monkeypatch.setattr(owner, name, namespace[name])


def test_http_endpoint_refuses_invalid_grant_without_effect_or_admission(system, monkeypatch):
    service, client, _, world, backend = system
    wire = approved(system, "generic_http_call")
    forged = deepcopy(wire)
    forged["authority_context"]["signature"] = "invalid"
    before = _state(backend)
    if MUTANT:
        lookup = service._lookup
        monkeypatch.setattr(service, "_lookup", lambda _wire: lookup(wire))
    response = client.post("/api/v1/effects/execute", json=forged)
    if response.status_code == 202:
        service.run_once(wire["operation_id"])
    _assert_refused(response.status_code == 403, world.posts + world.gets, before, _state(backend))
    assert before["reservations"] == [] and before["w2_positive_v1_outbox"] == []
    assert before["b1_effect_commands"] == []


def test_run_once_refuses_unauthenticated_native_permit_without_effect(system, monkeypatch):
    service, client, _, world, backend = system
    wire = approved(system, "stripe_charge")
    assert client.post("/api/v1/effects/execute", json=wire).status_code == 202
    with backend._connection() as db:
        db.execute("UPDATE w2_positive_v1_permits SET signature=? WHERE operation_key=?",
                   (b"invalid", wire["operation_id"]))
    before = _state(backend)
    if MUTANT:
        actual = service.authority.private_key
        class UnverifiedKey:
            def public_key(self):
                return self
            def verify(self, *args):
                return None
            def sign(self, data):
                return actual.sign(data)
        monkeypatch.setattr(service.authority, "private_key", UnverifiedKey())
    refused = False
    try:
        service.run_once(wire["operation_id"])
    except RuntimeError as exc:
        assert "signature invalid" in str(exc)
        refused = True
    _assert_refused(refused, world.posts + world.gets, before, _state(backend))
    assert len(before["reservations"]) == 1  # Pre-existing authorized admission.
    assert before["w2_positive_v1_outbox"] == []


def test_native_positive_dispatch_refuses_invalid_grant(tmp_path, monkeypatch, observed_b3):
    with native_lab(tmp_path) as lab:
        gate = NativePositiveG4A(lab.auth)
        payload = command()
        grant = gate.evaluate_and_grant("op-native-negative", "eval", payload, resource_id=RESOURCE)
        grant = {**grant, "signature": b"invalid"}
        before = _state(lab.backend)
        if MUTANT:
            monkeypatch.setattr(gate, "_require_grant", lambda *args: None)
        refused = False
        try:
            gate.dispatch_to_b3("op-native-negative", "idem", payload, grant, lab.provider)
        except RuntimeError as exc:
            assert "signature invalid" in str(exc)
            refused = True
        _assert_refused(refused, observed_b3, before, _state(lab.backend))
        assert before["w2_positive_v1_outbox"] == []


def test_proof_provider_direct_refuses_without_claim(tmp_path, observed_b3):
    with native_lab(tmp_path) as lab:
        provider = ProofProvider(str(tmp_path / "raw-provider.db"))
        try:
            before = _state(lab.backend)
            if MUTANT:
                provider.allow_unclaimed_execution = True
            refused = False
            try:
                provider.execute("op-raw", "idem", command())
            except RuntimeError as exc:
                assert "authenticated kernel claim required" in str(exc)
                refused = True
            _assert_refused(refused, observed_b3, before, _state(lab.backend))
            assert provider.execute_count("op-raw") == 0
        finally:
            provider.conn.close()


def test_kernel_provider_direct_refuses_without_claim(tmp_path, observed_b3):
    with native_lab(tmp_path) as lab:
        before = _state(lab.backend)
        if MUTANT:
            lab.provider.claim_verifier = None
            lab.provider.allow_unclaimed_execution = True
        refused = False
        try:
            lab.provider.execute("op-kernel-raw", "idem", command())
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
            refused = True
        _assert_refused(refused, observed_b3, before, _state(lab.backend))
        assert lab.provider.execute_count("op-kernel-raw") == 0


def _legacy_state(i8, gate):
    return {
        "budget": i8.conn.execute("SELECT * FROM ledger ORDER BY operation_id").fetchall(),
        "pending": [tuple(row) for row in gate.conn.execute(
            "SELECT operation_id, consumed, dispatch_claim, idempotency_key FROM permits ORDER BY operation_id")],
    }


def _legacy_system(path, cleanup):
    i8, rc01, provider = I8Ledger(str(path / "i8.db")), RC01Lab(str(path / "rc01.db")), ProofProvider(str(path / "provider.db"))
    for obj in (i8, rc01, provider):
        cleanup.callback(obj.conn.close)
    gate = PositiveG4A(i8, rc01, provider, str(path / "permits.db"))
    cleanup.callback(gate.conn.close)
    gate.admit_and_reserve("op-legacy", "eval", {"amount": 100})
    return i8, rc01, provider, gate


def test_legacy_positive_dispatch_refuses_before_pending_send(tmp_path, monkeypatch, observed_b3):
    with ExitStack() as cleanup:
        i8, _, provider, gate = _legacy_system(tmp_path, cleanup)
        before = _legacy_state(i8, gate)
        if MUTANT:
            _remove_fail_closed_guard(PositiveG4A, "consume_and_dispatch", monkeypatch)
        refused = False
        try:
            gate.consume_and_dispatch("op-legacy", {"amount": 100}, "idem-legacy")
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
            refused = True
        _assert_refused(refused, observed_b3, before, _legacy_state(i8, gate))
        assert provider.execute_count("op-legacy") == 0


def test_legacy_positive_recovery_refuses_without_effect(tmp_path, monkeypatch, observed_b3):
    with ExitStack() as cleanup:
        i8, _, provider, gate = _legacy_system(tmp_path, cleanup)
        gate.conn.execute("UPDATE permits SET consumed=1, dispatch_claim=1, idempotency_key=?, payload_json=? WHERE operation_id=?",
                          ("idem-legacy", '{"amount":100}', "op-legacy"))
        gate.conn.commit()
        before = _legacy_state(i8, gate)
        if MUTANT:
            _remove_fail_closed_guard(PositiveG4A, "_repair_dispatch_claim", monkeypatch)
            # The lower-level provider still refuses. Removing this second guard
            # isolates the old recovery path's own authentication boundary.
            provider.allow_unclaimed_execution = True
        refused = False
        try:
            gate.recover()
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
            refused = True
        _assert_refused(refused, observed_b3, before, _legacy_state(i8, gate))
        assert provider.execute_count("op-legacy") == 0


def test_legacy_s1_refuses_before_reserving_or_queueing(tmp_path, monkeypatch, observed_b3):
    with ExitStack() as cleanup:
        # Capture handles even if the historical runner aborts mid-initialization.
        for name in ("ProofProvider", "I8Ledger", "RC01Lab"):
            cls = getattr(lab_s1_bridge, name)
            def tracked(*args, _cls=cls, **kwargs):
                obj = _cls(*args, **kwargs)
                cleanup.callback(obj.conn.close)
                return obj
            monkeypatch.setattr(lab_s1_bridge, name, tracked)
        if MUTANT:
            _remove_fail_closed_guard(lab_s1_bridge, "run_lab_s1_scenario", monkeypatch)
        refused = False
        try:
            lab_s1_bridge.run_lab_s1_scenario("op-old-s1", str(tmp_path))
        except RuntimeError as exc:
            assert "authenticated kernel claim required" in str(exc)
            refused = True
        changed = {}
        for filename, table in (("i8.db", "ledger"), ("rc01.db", "rc01_states")):
            if Path(tmp_path, filename).exists():
                with closing(sqlite3.connect(str(tmp_path / filename))) as db:
                    changed[table] = db.execute("SELECT * FROM " + table).fetchall()
        _assert_refused(refused, observed_b3, {}, changed)
