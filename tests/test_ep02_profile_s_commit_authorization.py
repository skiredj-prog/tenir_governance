"""Profile S commit authorization: binding, durability and consumption."""
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import multiprocessing as mp
import os
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tenir_governance.experimental.ep01_evidence import (
    SignedClaim,
    canonical_claim_bytes,
)
from tenir_governance.experimental.ep01_policy import Claim, Verdict
from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_commit_authorization import (
    AuthorizationConsumptionConflict,
    AuthorizationDecision,
    AuthorizationRequestConflict,
    CommitAuthorizationRequest,
    CommitLookupStatus,
    ConsumptionDecision,
    ProfileSCommitAuthority,
    canonical_amount,
    operation_binding,
    verify_authorization_receipt,
    verify_consumption_receipt,
)
from tenir_governance.experimental.ep02_durable_cas import (
    DurableCASResult,
    StoreIntegrityError,
)
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
    verified_bound_preflight,
)


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _raw_public(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _raw_private(key):
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _snapshot(root, issuer, revision, *, revoked=()):
    state = TrustState(
        revision, "policy-v1", (("issuer", "contracts"),), tuple(revoked),
    )
    skeleton = SignedTrustState(
        state, b"", "root", (("issuer", _raw_public(issuer)),),
    )
    return SignedTrustState(
        state, root.sign(bound_snapshot_bytes(skeleton)), "root",
        skeleton.issuer_keys,
    )


def _proof(issuer):
    claim = Claim("document-B", "issuer", "document-A", NOW)
    return SignedClaim(claim, issuer.sign(canonical_claim_bytes(claim)))


def _setup(path):
    root = Ed25519PrivateKey.generate()
    issuer = Ed25519PrivateKey.generate()
    receipt_key = Ed25519PrivateKey.generate()
    service = ProfileSCommitAuthority.provision(
        path,
        authority_id="authority-a",
        trust_profile_id="institutional-roots",
        trust_generation=1,
        roots={"root": root.public_key()},
        receipt_key_id="receipt-key-a",
        receipt_key_generation=1,
        receipt_signing_key=receipt_key,
    )
    snapshot = _snapshot(root, issuer, 1)
    bootstrap = service.anchor.bootstrap("bootstrap-1", snapshot)
    assert bootstrap.result == DurableCASResult.APPLIED
    proof = _proof(issuer)
    verdict, _, finding = verified_bound_preflight(
        proof, prior="document-A", scope="contracts", now=NOW,
        trust=snapshot, root_keys={"root": root.public_key()},
    )
    assert verdict == Verdict.ADMISSIBLE
    binding = operation_binding(
        authority_id="authority-a",
        profile_id="institutional-roots",
        profile_generation=1,
        institutional_scope="institution-a",
        operation_id="operation-1",
        idempotency_key="idem-1",
        passage_id="passage-1",
        agent_id="agent-1",
        action_type="payment",
        resource_id="budget",
        amount="10.00",
        payload_digest=sha256(b'{"amount":"10"}').hexdigest(),
        finding=finding,
    )
    request = CommitAuthorizationRequest("authorization-1", binding)
    return service, root, issuer, receipt_key, snapshot, proof, finding, request


def _trusted(receipt_key):
    return {("receipt-key-a", 1): receipt_key.public_key()}


def _authorize_worker(
    path, signing_raw, request, finding, proof, snapshot, barrier, queue,
):
    signing_key = Ed25519PrivateKey.from_private_bytes(signing_raw)
    service = ProfileSCommitAuthority.open(
        path, receipt_signing_key=signing_key,
    )
    barrier.wait(timeout=20)
    receipt = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    queue.put((receipt.decision.value, receipt.sequence, receipt.replayed,
               receipt.signature))


def _consume_worker(
    path, signing_raw, request, authorization, claim_id, barrier, queue,
):
    signing_key = Ed25519PrivateKey.from_private_bytes(signing_raw)
    service = ProfileSCommitAuthority.open(
        path, receipt_signing_key=signing_key,
    )
    barrier.wait(timeout=20)
    try:
        receipt = service.consume(
            request, authorization, dispatch_claim_id=claim_id,
        )
        queue.put(("receipt", receipt.decision.value, receipt.dispatch_claim_id))
    except AuthorizationConsumptionConflict:
        queue.put(("conflict", None, claim_id))


def _crash_authorize_worker(
    path, signing_raw, request, finding, proof, snapshot, point,
):
    class CrashAtPoint(ProfileSCommitAuthority):
        def _fault(self, current):
            if current == point:
                os._exit(73)

    service = CrashAtPoint.open(
        path,
        receipt_signing_key=Ed25519PrivateKey.from_private_bytes(signing_raw),
    )
    service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )


def _crash_consume_worker(
    path, signing_raw, request, authorization, point,
):
    class CrashAtPoint(ProfileSCommitAuthority):
        def _fault(self, current):
            if current == point:
                os._exit(73)

    service = CrashAtPoint.open(
        path,
        receipt_signing_key=Ed25519PrivateKey.from_private_bytes(signing_raw),
    )
    service.consume(
        request, authorization, dispatch_claim_id="dispatch-claim-crash",
    )


def test_preflight_alone_creates_no_authorization_or_consumption(tmp_path):
    service, *_, request = _setup(tmp_path / "authority.sqlite3")

    assert service.lookup_authorization(request).status == CommitLookupStatus.NOT_RECORDED
    assert service.lookup_consumption(request).status == CommitLookupStatus.NOT_RECORDED


def test_exact_authorization_is_signed_durable_and_single_consumption(tmp_path):
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )

    assert authorization.decision == AuthorizationDecision.AUTHORIZED
    assert authorization.reason == "COMMIT_AUTHORIZED"
    assert not authorization.replayed
    assert verify_authorization_receipt(
        authorization, request, trusted_keys=_trusted(receipt_key),
    )

    consumed = service.consume(
        request, authorization, dispatch_claim_id="dispatch-claim-1",
    )
    replay = ProfileSCommitAuthority.open(
        service.anchor.path, receipt_signing_key=receipt_key,
    ).consume(request, authorization, dispatch_claim_id="dispatch-claim-1")

    assert consumed.decision == ConsumptionDecision.CONSUMED
    assert not consumed.replayed
    assert replay.replayed
    assert replay.sequence == consumed.sequence
    assert replay.signature == consumed.signature
    assert verify_consumption_receipt(
        consumed, request, authorization, trusted_keys=_trusted(receipt_key),
    )


def test_open_validates_authorizations_and_consumptions_on_one_snapshot(
    tmp_path, monkeypatch,
):
    path = tmp_path / "authority.sqlite3"
    writer, _, _, receipt_key, snapshot, proof, finding, request = _setup(path)
    original = ProfileSCommitAuthority._validate_rows
    advanced = False
    callback_errors = []

    def validate_while_writer_advances(self, connection):
        def advance_before_consumption_scan(statement):
            nonlocal advanced
            if advanced or "FROM commit_consumptions" not in statement:
                return
            advanced = True
            connection.set_trace_callback(None)
            try:
                authorization = writer.authorize(
                    request,
                    finding=finding,
                    claim=proof,
                    current_snapshot=snapshot,
                    now=NOW,
                )
                writer.consume(
                    request,
                    authorization,
                    dispatch_claim_id="dispatch-during-open",
                )
            except Exception as error:  # surfaced below; SQLite swallows callback errors
                callback_errors.append(error)

        connection.set_trace_callback(advance_before_consumption_scan)
        try:
            return original(self, connection)
        finally:
            connection.set_trace_callback(None)

    monkeypatch.setattr(
        ProfileSCommitAuthority, "_validate_rows", validate_while_writer_advances,
    )

    reopened = ProfileSCommitAuthority.open(
        path, receipt_signing_key=receipt_key,
    )

    assert advanced
    assert callback_errors == []
    assert reopened.lookup_authorization(request).status == CommitLookupStatus.RECORDED
    assert reopened.lookup_consumption(request).status == CommitLookupStatus.RECORDED


@pytest.mark.parametrize("field,value", [
    ("institutional_scope", "institution-b"),
    ("operation_id", "operation-2"),
    ("idempotency_key", "idem-2"),
    ("passage_id", "passage-2"),
    ("agent_id", "agent-2"),
    ("action_type", "refund"),
    ("resource_id", "another-budget"),
    ("amount", "11"),
    ("payload_digest", "f" * 64),
    ("claim_digest", "e" * 64),
    ("prior_document", "document-X"),
    ("evidence_scope", "payments"),
    ("policy_version", "policy-v2"),
])
def test_request_id_cannot_be_rebound_to_substituted_operation(
    tmp_path, field, value,
):
    service, _, _, _, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    first = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    assert first.decision == AuthorizationDecision.AUTHORIZED
    changed = replace(request, binding=replace(request.binding, **{field: value}))

    with pytest.raises(AuthorizationRequestConflict):
        service.authorize(
            changed, finding=finding, claim=proof,
            current_snapshot=snapshot, now=NOW,
        )


def test_stale_preflight_is_durably_rejected_after_trust_advance(tmp_path):
    service, root, issuer, _, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    first = service.anchor.get()
    advanced = _snapshot(root, issuer, 2)
    assert service.anchor.compare_and_swap(
        "trust-advance-2", first, advanced,
    ).result == DurableCASResult.APPLIED

    receipt = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    replay = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )

    assert receipt.decision == AuthorizationDecision.REJECTED
    assert receipt.reason == "TRUST_CHECKPOINT_STALE"
    assert replay.replayed
    assert replay.signature == receipt.signature


def test_authorization_becomes_stale_if_trust_advances_before_consumption(tmp_path):
    service, root, issuer, _, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    first = service.anchor.get()
    assert service.anchor.compare_and_swap(
        "trust-advance-2", first, _snapshot(root, issuer, 2),
    ).result == DurableCASResult.APPLIED

    consumed = service.consume(
        request, authorization, dispatch_claim_id="dispatch-claim-1",
    )
    assert consumed.decision == ConsumptionDecision.STALE
    assert consumed.reason == "TRUST_CHECKPOINT_CHANGED"


def test_rejected_authorization_cannot_be_consumed(tmp_path):
    service, _, _, _, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    changed = replace(
        request, binding=replace(request.binding, claim_digest="e" * 64),
    )
    rejected = service.authorize(
        changed, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    assert rejected.decision == AuthorizationDecision.REJECTED
    assert rejected.reason == "PREFLIGHT_BINDING_MISMATCH"

    consumed = service.consume(
        changed, rejected, dispatch_claim_id="dispatch-claim-1",
    )
    assert consumed.decision == ConsumptionDecision.REJECTED
    assert consumed.reason == "AUTHORIZATION_NOT_GRANTED"


def test_tampered_or_foreign_receipt_never_consumes(tmp_path):
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    tampered = replace(authorization, binding_digest="f" * 64)

    assert not verify_authorization_receipt(
        tampered, request, trusted_keys=_trusted(receipt_key),
    )
    with pytest.raises(ValueError, match="verification failed"):
        service.consume(request, tampered, dispatch_claim_id="dispatch-claim-1")
    assert service.lookup_consumption(request).status == CommitLookupStatus.NOT_RECORDED


def test_lost_authorization_reply_is_resolved_by_lookup(tmp_path):
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )

    class LostReply(ProfileSCommitAuthority):
        def _fault(self, point):
            if point == "after_authorization_commit_before_reply":
                raise ConnectionError("reply lost")

    lost = LostReply.open(service.anchor.path, receipt_signing_key=receipt_key)
    with pytest.raises(ConnectionError, match="reply lost"):
        lost.authorize(
            request, finding=finding, claim=proof,
            current_snapshot=snapshot, now=NOW,
        )

    lookup = service.lookup_authorization(request)
    assert lookup.status == CommitLookupStatus.RECORDED
    assert lookup.receipt.decision == AuthorizationDecision.AUTHORIZED
    assert lookup.receipt.replayed


def test_lost_consumption_reply_is_resolved_without_second_claim(tmp_path):
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(
        tmp_path / "authority.sqlite3"
    )
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )

    class LostReply(ProfileSCommitAuthority):
        def _fault(self, point):
            if point == "after_consumption_commit_before_reply":
                raise ConnectionError("reply lost")

    lost = LostReply.open(service.anchor.path, receipt_signing_key=receipt_key)
    with pytest.raises(ConnectionError, match="reply lost"):
        lost.consume(
            request, authorization, dispatch_claim_id="dispatch-claim-1",
        )

    lookup = service.lookup_consumption(request)
    assert lookup.status == CommitLookupStatus.RECORDED
    assert lookup.receipt.decision == ConsumptionDecision.CONSUMED
    assert lookup.receipt.dispatch_claim_id == "dispatch-claim-1"
    with pytest.raises(AuthorizationConsumptionConflict):
        service.consume(
            request, authorization, dispatch_claim_id="dispatch-claim-2",
        )


def test_two_processes_authorize_same_request_once(tmp_path):
    path = tmp_path / "authority.sqlite3"
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(path)
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    barrier = ctx.Barrier(2)
    args = (
        str(path), _raw_private(receipt_key), request, finding, proof,
        snapshot, barrier, queue,
    )
    processes = [ctx.Process(target=_authorize_worker, args=args) for _ in range(2)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert {result[0] for result in results} == {"AUTHORIZED"}
    assert len({result[1] for result in results}) == 1
    assert sorted(result[2] for result in results) == [False, True]
    assert len({result[3] for result in results}) == 1
    assert service.lookup_authorization(request).status == CommitLookupStatus.RECORDED


def test_two_processes_cannot_consume_for_different_dispatch_claims(tmp_path):
    path = tmp_path / "authority.sqlite3"
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(path)
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    barrier = ctx.Barrier(2)
    common = (str(path), _raw_private(receipt_key), request, authorization)
    processes = [
        ctx.Process(
            target=_consume_worker,
            args=(*common, f"dispatch-claim-{index}", barrier, queue),
        )
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sorted(result[0] for result in results) == ["conflict", "receipt"]
    assert [result[1] for result in results if result[0] == "receipt"] == ["CONSUMED"]


@pytest.mark.parametrize("point,recorded", [
    ("after_authorization_begin", False),
    ("after_authorization_insert", False),
    ("before_authorization_commit", False),
    ("after_authorization_commit_before_reply", True),
])
def test_hard_exit_recovers_whole_authorization_or_nothing(
    tmp_path, point, recorded,
):
    path = tmp_path / "authority.sqlite3"
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(path)
    ctx = mp.get_context("spawn")
    process = ctx.Process(
        target=_crash_authorize_worker,
        args=(str(path), _raw_private(receipt_key), request, finding, proof,
              snapshot, point),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 73

    reopened = ProfileSCommitAuthority.open(
        path, receipt_signing_key=receipt_key,
    )
    lookup = reopened.lookup_authorization(request)
    assert (lookup.status == CommitLookupStatus.RECORDED) is recorded
    if recorded:
        assert lookup.receipt.decision == AuthorizationDecision.AUTHORIZED
    else:
        retry = reopened.authorize(
            request, finding=finding, claim=proof,
            current_snapshot=snapshot, now=NOW,
        )
        assert retry.decision == AuthorizationDecision.AUTHORIZED
        assert not retry.replayed


@pytest.mark.parametrize("point,recorded", [
    ("after_consumption_begin", False),
    ("after_consumption_insert", False),
    ("before_consumption_commit", False),
    ("after_consumption_commit_before_reply", True),
])
def test_hard_exit_recovers_whole_consumption_or_nothing(
    tmp_path, point, recorded,
):
    path = tmp_path / "authority.sqlite3"
    service, _, _, receipt_key, snapshot, proof, finding, request = _setup(path)
    authorization = service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    ctx = mp.get_context("spawn")
    process = ctx.Process(
        target=_crash_consume_worker,
        args=(str(path), _raw_private(receipt_key), request, authorization, point),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 73

    reopened = ProfileSCommitAuthority.open(
        path, receipt_signing_key=receipt_key,
    )
    lookup = reopened.lookup_consumption(request)
    assert (lookup.status == CommitLookupStatus.RECORDED) is recorded
    if recorded:
        assert lookup.receipt.decision == ConsumptionDecision.CONSUMED
    else:
        retry = reopened.consume(
            request, authorization,
            dispatch_claim_id="dispatch-claim-crash",
        )
        assert retry.decision == ConsumptionDecision.CONSUMED
        assert not retry.replayed


def test_database_contains_payload_digest_but_not_payload_bytes(tmp_path):
    path = tmp_path / "authority.sqlite3"
    service, _, _, _, snapshot, proof, finding, request = _setup(path)
    service.authorize(
        request, finding=finding, claim=proof,
        current_snapshot=snapshot, now=NOW,
    )
    connection = sqlite3.connect(path)
    envelope = connection.execute(
        "SELECT envelope_json FROM commit_authorizations"
    ).fetchone()[0]
    connection.close()

    assert request.binding.payload_digest in envelope
    assert '{"amount":"10"}' not in envelope


def test_open_rejects_unpinned_receipt_private_key(tmp_path):
    path = tmp_path / "authority.sqlite3"
    _setup(path)
    with pytest.raises(StoreIntegrityError, match="does not match profile"):
        ProfileSCommitAuthority.open(
            path, receipt_signing_key=Ed25519PrivateKey.generate(),
        )


def test_missing_immutability_guard_fails_closed_on_reopen(tmp_path):
    path = tmp_path / "authority.sqlite3"
    _, _, _, receipt_key, *_ = _setup(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER commit_consumptions_no_delete")
    connection.commit()
    connection.close()

    with pytest.raises(StoreIntegrityError, match="guards are missing"):
        ProfileSCommitAuthority.open(path, receipt_signing_key=receipt_key)


@pytest.mark.parametrize("value,expected", [
    (0, "0"), ("0.00", "0"), (10, "10"), ("10.00", "10"),
    ("0.1250", "0.125"),
])
def test_amount_canonicalization(value, expected):
    assert canonical_amount(value) == expected


@pytest.mark.parametrize("value", [True, -1, "NaN", "Infinity", "-Infinity"])
def test_amount_rejects_bool_negative_and_nonfinite(value):
    with pytest.raises(ValueError, match="amount"):
        canonical_amount(value)
