"""External EP-02 CAS: authenticated writes, receipts and partition recovery."""
from dataclasses import replace
import multiprocessing as mp

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tenir_governance.experimental.ep02_binding import TrustState
from tenir_governance.experimental.ep02_durable_cas import (
    DurableAnchorCAS,
    DurableCASResult,
    LookupStatus,
    RequestIdConflict,
)
from tenir_governance.experimental.ep02_rotation import TrustCheckpoint
from tenir_governance.experimental.ep02_signed_trust import (
    SignedTrustState,
    bound_snapshot_bytes,
)
from tenir_governance.experimental.ep02_external_cas import (
    ExternalCASClient,
    ExternalAuthorityService,
    ExternalOutcomeUnknown,
    ExternalProtocolError,
    build_external_request,
    build_lookup_query,
    decode_lookup_query,
    decode_request,
    decode_receipt,
    encode_lookup_query,
    encode_request,
    new_lookup_challenge,
    verify_receipt,
)


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


def _signed(root, revision):
    issuer = Ed25519PrivateKey.generate()
    state = TrustState(revision, "policy-v1", (("issuer", "contracts"),))
    template = SignedTrustState(
        state, b"", "root", (("issuer", _raw_public(issuer)),),
    )
    return SignedTrustState(
        state, root.sign(bound_snapshot_bytes(template)), "root",
        template.issuer_keys,
    )


def _service(tmp_path):
    root = Ed25519PrivateKey.generate()
    client = Ed25519PrivateKey.generate()
    anchor = Ed25519PrivateKey.generate()
    store = DurableAnchorCAS.provision(
        tmp_path / "authority.sqlite3",
        authority_id="external-authority",
        trust_profile_id="institutional-roots",
        trust_generation=1,
        roots={"root": root.public_key()},
    )
    service = ExternalAuthorityService(
        store,
        anchor_id="anchor-1",
        signing_key=anchor,
        client_keys={("client-a", 1): client.public_key()},
    )
    return service, store, root, client, anchor.public_key()


def _external_service_worker(
    path, request_wire, client_id, generation, client_public_raw,
    anchor_private_raw, queue, barrier,
):
    store = DurableAnchorCAS.open(path)
    service = ExternalAuthorityService(
        store,
        anchor_id="anchor-1",
        signing_key=Ed25519PrivateKey.from_private_bytes(anchor_private_raw),
        client_keys={
            (client_id, generation):
            Ed25519PublicKey.from_public_bytes(client_public_raw)
        },
    )
    barrier.wait(timeout=20)
    receipt = decode_receipt(service.execute(request_wire))
    queue.put((receipt.result.value, receipt.reason, receipt.sequence))


def test_authenticated_bootstrap_and_cas_return_verifiable_receipts(tmp_path):
    service, store, root, client, anchor_key = _service(tmp_path)
    bootstrap = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    boot_receipt = decode_receipt(service.execute(encode_request(bootstrap)))
    assert verify_receipt(
        boot_receipt, request=bootstrap, trusted_anchors={"anchor-1": anchor_key},
    )
    assert boot_receipt.result == DurableCASResult.APPLIED

    expected = store.get()
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="rotation-1", operation="CAS",
        expected=expected, successor=_signed(root, 2),
    )
    receipt = decode_receipt(service.execute(encode_request(request)))
    assert verify_receipt(
        receipt, request=request, trusted_anchors={"anchor-1": anchor_key},
    )
    assert receipt.result == DurableCASResult.APPLIED
    assert receipt.result_checkpoint.revision == 2


def test_exact_retry_replays_original_external_outcome(tmp_path):
    service, store, root, client, anchor_key = _service(tmp_path)
    bootstrap = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    first = decode_receipt(service.execute(bootstrap))
    retry = decode_receipt(service.execute(bootstrap))
    assert verify_receipt(
        retry, request=bootstrap, trusted_anchors={"anchor-1": anchor_key},
    )
    assert retry.result == first.result == DurableCASResult.APPLIED
    assert retry.sequence == first.sequence
    assert retry.replayed
    assert store.request_count() == 1


def test_client_identity_generation_and_request_context_are_bound(tmp_path):
    service, store, root, client, _ = _service(tmp_path)
    original = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    assert decode_receipt(service.execute(original)).result == DurableCASResult.APPLIED
    changed = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 2),
    )
    with pytest.raises(RequestIdConflict):
        service.execute(changed)
    assert store.get().revision == 1

    generation_two = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=2, request_id="bootstrap-2", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 2),
    )
    with pytest.raises(ExternalProtocolError, match="client"):
        service.execute(generation_two)


def test_unauthorized_or_tampered_request_cannot_mutate_authority(tmp_path):
    service, store, root, client, _ = _service(tmp_path)
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    other = Ed25519PrivateKey.generate()
    forged = replace(request, client_signature=other.sign(request.payload))
    with pytest.raises(ExternalProtocolError, match="signature"):
        service.execute(forged)
    assert store.get() is None
    assert store.request_count() == 0


def test_lost_reply_after_commit_resolves_to_recorded_original(tmp_path):
    service, store, root, client, anchor_key = _service(tmp_path)
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    class DropReply:
        def execute(self, payload):
            service.execute(payload)
            raise TimeoutError("reply lost")

        def lookup(self, payload):
            return service.lookup(payload)

    client_api = ExternalCASClient(
        DropReply(), trusted_anchors={"anchor-1": anchor_key},
    )
    with pytest.raises(ExternalOutcomeUnknown):
        client_api.execute(request)
    challenge = bytes(range(32))
    query = build_lookup_query(client, request=request, challenge=challenge)
    receipt = client_api.lookup(query)
    assert receipt.lookup_status == LookupStatus.RECORDED
    assert receipt.result == DurableCASResult.APPLIED
    assert store.request_count() == 1


def test_partition_before_service_reports_not_recorded_without_proving_failure(tmp_path):
    service, store, root, client, anchor_key = _service(tmp_path)
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    class Partitioned:
        def execute(self, payload):
            raise ConnectionError("partitioned")

        def lookup(self, payload):
            return service.lookup(payload)

    client_api = ExternalCASClient(
        Partitioned(), trusted_anchors={"anchor-1": anchor_key},
    )
    with pytest.raises(ExternalOutcomeUnknown):
        client_api.execute(request)
    challenge = b"q" * 32
    query = build_lookup_query(client, request=request, challenge=challenge)
    receipt = client_api.lookup(query)
    assert receipt.lookup_status == LookupStatus.NOT_RECORDED
    assert receipt.result is None
    retry = decode_receipt(service.execute(request))
    assert retry.result == DurableCASResult.APPLIED
    assert store.request_count() == 1


def test_lookup_challenge_and_anchor_signature_prevent_receipt_substitution(tmp_path):
    service, _, root, client, anchor_key = _service(tmp_path)
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    service.execute(request)
    old_challenge = b"a" * 32
    receipt = decode_receipt(service.lookup(build_lookup_query(
        client, request=request, challenge=old_challenge,
    )))
    assert not verify_receipt(
        receipt, request=request, challenge=b"b" * 32,
        trusted_anchors={"anchor-1": anchor_key},
    )
    tampered = replace(receipt, current_checkpoint=TrustCheckpoint(99, "f" * 64))
    assert not verify_receipt(
        tampered, request=request, challenge=old_challenge,
        trusted_anchors={"anchor-1": anchor_key},
    )


def test_competing_external_service_processes_share_one_atomic_authority(tmp_path):
    root = Ed25519PrivateKey.generate()
    anchor = Ed25519PrivateKey.generate()
    clients = [Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()]
    path = tmp_path / "authority.sqlite3"
    store = DurableAnchorCAS.provision(
        path,
        authority_id="external-authority",
        trust_profile_id="institutional-roots",
        trust_generation=1,
        roots={"root": root.public_key()},
    )
    bootstrap = store.bootstrap("local-bootstrap", _signed(root, 1))
    assert bootstrap.result == DurableCASResult.APPLIED
    expected = store.get()
    requests = [
        build_external_request(
            client,
            authority_id="external-authority",
            client_id=f"client-{index}",
            client_generation=1,
            request_id="rotation-1",
            operation="CAS",
            expected=expected,
            successor=_signed(root, 2),
        )
        for index, client in enumerate(clients)
    ]
    context = mp.get_context("spawn")
    barrier, queue = context.Barrier(3), context.Queue()
    workers = [
        context.Process(
            target=_external_service_worker,
            args=(
                str(path), encode_request(request), f"client-{index}", 1,
                _raw_public(client), _raw_private(anchor), queue, barrier,
            ),
        )
        for index, (client, request) in enumerate(zip(clients, requests))
    ]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        for worker in workers:
            worker.join(timeout=30)
            assert worker.exitcode == 0
        results = [queue.get(timeout=5) for _ in workers]
        assert sorted(item[0] for item in results) == ["APPLIED", "CONFLICT"]
        assert len({item[2] for item in results}) == 2
        assert DurableAnchorCAS.open(path).get().revision == 2
        assert DurableAnchorCAS.open(path).request_count() == 3
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            worker.close()
        queue.close()
        queue.join_thread()


def test_lookup_query_requires_fresh_bounded_challenge(tmp_path):
    _, _, root, client, _ = _service(tmp_path)
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    with pytest.raises(ValueError, match="challenge"):
        build_lookup_query(client, request=request, challenge=b"short")
    challenges = {new_lookup_challenge() for _ in range(32)}
    assert len(challenges) == 32
    assert all(len(challenge) == 32 for challenge in challenges)


def test_bool_revision_is_rejected_before_authority_mutation(tmp_path):
    _, store, root, _, _ = _service(tmp_path)
    state = TrustState(True, "policy-v1", (("issuer", "contracts"),))
    template = SignedTrustState(state, b"", "root", ())
    with pytest.raises(ValueError, match="revision"):
        root.sign(bound_snapshot_bytes(template))
    assert store.get() is None
    assert store.request_count() == 0


def test_request_and_lookup_wire_formats_are_canonical_and_bounded(tmp_path):
    _, _, root, client, _ = _service(tmp_path)
    request = build_external_request(
        client, authority_id="external-authority", client_id="client-a",
        client_generation=1, request_id="bootstrap-1", operation="BOOTSTRAP",
        expected=None, successor=_signed(root, 1),
    )
    assert decode_request(encode_request(request)) == request
    query = build_lookup_query(client, request=request, challenge=b"c" * 32)
    decoded = decode_lookup_query(encode_lookup_query(query))
    assert decoded == query
    with pytest.raises(ExternalProtocolError):
        decode_request(b"{}")
    with pytest.raises(ExternalProtocolError):
        decode_lookup_query(b"{}")
