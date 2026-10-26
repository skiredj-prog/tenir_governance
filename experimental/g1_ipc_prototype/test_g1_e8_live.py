"""Exploratory INT-E8-01 loopback tests: real crypto/HTTP/SQLite, SAME HOST.

NEVER count these as an independent-witness security PASS. Missing real G1
one-time grants, full-state attestation, isolation, provider effects, TLS.
"""
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from e8_lab import WitnessClient, WitnessStore, start_loopback_test_server
from g1_e8_guarded import initialize_guarded, process_guarded
from g1_e8_snapshot import business_digest
from g1_ipc import initialize
from test_g1_ipc import KEY, UID, request


@pytest.fixture
def lab(tmp_path):
    engine_key = Ed25519PrivateKey.generate()
    witness_key = Ed25519PrivateKey.generate()
    store = WitnessStore(str(tmp_path/'witness.sqlite'), witness_key, engine_key.public_key())
    db = str(tmp_path/'engine.sqlite')
    initialize(db)
    with sqlite3.connect(db) as con:
        genesis_digest = business_digest(con)
    initial = store.provision_for_test(
        'test', 'lab-store', epoch=5, state_digest=genesis_digest
    )
    initialize_guarded(db, realm='test', store_id='lab-store', initial_head=initial)
    server, url = start_loopback_test_server(store)
    client = WitnessClient(url, engine_key, witness_key.public_key())
    yield {'db':db, 'store':store,'initial':initial,'client':client,
           'engine_key':engine_key, 'witness_key':witness_key}
    server.shutdown()
    server.server_close()


def submit(lab, req, **kwargs):
    return process_guarded(lab['db'], req, UID, {UID:KEY.public_key()},
                           store_id='lab-store', witness=lab['client'], **kwargs)


def rows(lab):
    with sqlite3.connect(lab['db']) as con:
        return (con.execute('SELECT count(*) FROM g1_local_commits').fetchone()[0],
                con.execute('SELECT count(*) FROM g1_idempotency_ledger').fetchone()[0])


def local_head(lab):
    with sqlite3.connect(lab['db']) as con:
        return json.loads(con.execute('SELECT head_json FROM g1_e8_head').fetchone()[0])


def external_head(lab):
    return lab['store'].current_for_test('test','lab-store')


def test_int_e8_01a_rollback_epoch_refused_before_commit(lab):
    out = submit(lab, request('r_rollback','tx_rollback',epoch=4))
    assert out == {'decision':'HARD_HOLD','reason':'E8_ROLLBACK_DETECTED'}
    assert rows(lab) == (0,0)
    assert local_head(lab) == external_head(lab) == lab['initial']
    with sqlite3.connect(lab['db']) as con:
        assert con.execute('SELECT reason FROM g1_e8_hold').fetchone()[0]=='E8_ROLLBACK_DETECTED'


def test_int_e8_01b_equal_epoch_head_substitution_refused(lab):
    modified=dict(local_head(lab), state_digest='sha256:'+'f'*64)
    with sqlite3.connect(lab['db']) as con:
        con.execute('UPDATE g1_e8_head SET head_json=?',(json.dumps(modified),))
    out=submit(lab, request('r_corrupt','tx_corrupt',epoch=5))
    # Earlier local detection is stronger: the cached head itself was altered.
    assert out['reason']=='E8_LOCAL_STATE_DIVERGENCE'
    assert out['decision']=='HARD_HOLD'
    assert rows(lab)==(0,0)


def test_int_e8_01c_transport_failure_enters_hold(lab):
    lab['client'].url='http://127.0.0.1:1'  # unreachable loopback port
    out=submit(lab, request('r_net','tx_net',epoch=5))
    assert out=={'decision':'HOLD_GLOBAL','reason':'WITNESS_OUTCOME_UNKNOWN'}
    assert rows(lab)==(0,0)


def test_int_e8_01d_authenticated_cmt_precedes_single_sqlite_commit(lab):
    r=request('r_ok','tx_ok',epoch=5)
    out=submit(lab,r)
    assert out['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    assert out['replayed'] is False
    assert rows(lab)==(1,1)
    assert local_head(lab)==external_head(lab)
    assert external_head(lab)['sequence']==1
    again=submit(lab,r)
    assert again['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    assert again['replayed'] is True
    assert rows(lab)==(1,1)
    assert external_head(lab)['sequence']==1
    conflict=submit(lab,request('r_other','tx_ok',epoch=5))
    assert conflict['decision']=='CONFLICT'
    assert external_head(lab)['sequence']==1


def test_int_e8_01e_wrong_witness_key_causes_hold_without_commit(lab):
    lab['client']=WitnessClient(lab['client'].url,lab['engine_key'],Ed25519PrivateKey.generate().public_key())
    out=submit(lab,request('r_wrong_pin','tx_wrong_pin',epoch=5))
    assert out['decision']=='HOLD_GLOBAL'
    assert rows(lab)==(0,0)
    assert external_head(lab)['sequence']==1  # remote CAS may have succeeded before verification


def test_int_e8_01g_simulated_crash_after_witness_advance_before_sqlite(lab):
    r=request('r_crash','tx_crash',epoch=5)
    with pytest.raises(RuntimeError,match='INJECTED_CRASH_AFTER_PREAUTH'):
        submit(lab,r,crash_after_preauth=True)
    assert rows(lab)==(0,0)
    assert external_head(lab)['sequence']==1
    assert local_head(lab)['sequence']==0
    result=submit(lab,r)
    assert result['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    assert rows(lab)==(1,1)
    assert external_head(lab)==local_head(lab)
    assert external_head(lab)['sequence']==1


def test_sequential_different_transaction_head_advances(lab):
    assert submit(lab,request('r1','tx1',epoch=5))['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    assert submit(lab,request('r2','tx2',epoch=5))['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    assert rows(lab)==(2,2)
    assert external_head(lab)['sequence']==2
    assert external_head(lab)==local_head(lab)


def test_unix_socket_daemon_checks_peer_and_preauthed_local_commit(lab,tmp_path):
    """Actual AF_UNIX request/response; same UID, NOT an OS isolation test."""
    import os
    import threading
    import time
    from g1_ipc import client_request
    from g1_e8_guarded import serve_guarded
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    sock = str(tmp_path / 'g1.sock')
    pub = tmp_path / 'worker.pub'
    pub.write_bytes(KEY.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw))
    daemon = threading.Thread(target=serve_guarded,
                              args=(lab['db'],sock,str(pub),UID,'lab-store',lab['client']),
                              kwargs={'once':True},daemon=True)
    daemon.start()
    for _ in range(200):
        if os.path.exists(sock):
            break
        time.sleep(0.005)
    assert os.path.exists(sock)
    reply=client_request(sock,request('r_unix','tx_unix',epoch=5))
    daemon.join(timeout=2)
    assert not daemon.is_alive()
    assert reply['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    assert rows(lab)==(1,1)
    assert external_head(lab)['sequence']==1


def test_real_engine_must_detect_business_db_tamper_even_when_cached_head_untouched(lab):
    """P0 regression: a privileged DELETE without cached-head change is detected."""
    assert submit(lab,request('r_good','tx_good',epoch=5))['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    before = external_head(lab)
    with sqlite3.connect(lab['db']) as con:
        con.execute('DELETE FROM g1_local_commits')
    out=submit(lab,request('r_after_tamper','tx_other',epoch=5))
    assert out == {'decision':'HARD_HOLD','reason':'E8_LOCAL_STATE_DIVERGENCE'}
    assert external_head(lab) == before
    assert rows(lab) == (0,1)
    with sqlite3.connect(lab['db']) as con:
        assert con.execute('SELECT reason FROM g1_e8_hold').fetchone()[0] == 'E8_LOCAL_STATE_DIVERGENCE'


@pytest.mark.parametrize('mutation', [
    "UPDATE g1_local_commits SET payload_digest='sha256:" + "1"*64 + "'",
    "UPDATE g1_idempotency_ledger SET result_digest='sha256:" + "2"*64 + "'",
    "DELETE FROM g1_idempotency_ledger",
    "ALTER TABLE g1_local_commits ADD COLUMN privileged_field TEXT",
])
def test_privileged_row_or_schema_mutation_is_held_before_next_admission(lab, mutation):
    assert submit(lab,request('r1','tx1',epoch=5))['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    before_head = external_head(lab)
    with sqlite3.connect(lab['db']) as con:
        con.execute(mutation)
    before_rows = rows(lab)
    out = submit(lab,request('r2','tx2',epoch=5))
    assert out['decision']=='HARD_HOLD'
    assert out['reason'] in ('E8_LOCAL_STATE_DIVERGENCE','E8_LOCAL_STATE_UNVERIFIABLE')
    assert rows(lab)==before_rows
    assert external_head(lab)==before_head


def test_identical_replay_is_held_after_privileged_ledger_change(lab):
    req=request('r_repeat','tx_repeat',epoch=5)
    assert submit(lab,req)['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    with sqlite3.connect(lab['db']) as con:
        con.execute("UPDATE g1_idempotency_ledger SET status='REJECTED'")
    out=submit(lab,req)
    assert out == {'decision':'HARD_HOLD','reason':'E8_LOCAL_STATE_DIVERGENCE'}


def test_full_local_snapshot_matches_witness_after_two_commits(lab):
    for n in (1,2):
        assert submit(lab,request(f'req-{n}',f'tx-{n}',epoch=5))['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
        with sqlite3.connect(lab['db']) as con:
            assert business_digest(con)==external_head(lab)['state_digest']
        assert external_head(lab)['state_digest']==local_head(lab)['state_digest']


def test_unattested_genesis_is_refused_before_any_admission(tmp_path):
    db=str(tmp_path/'genesis.db')
    store=WitnessStore(str(tmp_path/'genesis-witness.db'),
                       Ed25519PrivateKey.generate(),Ed25519PrivateKey.generate().public_key())
    bad_genesis=store.provision_for_test('test','another-store',epoch=5)
    with pytest.raises(ValueError,match='E8_GENESIS_STATE_MISMATCH'):
        initialize_guarded(db,realm='test',store_id='another-store',initial_head=bad_genesis)


def test_modified_business_rows_and_forged_cached_head_still_rejected_by_witness(lab):
    assert submit(lab,request('r_good','tx_good',epoch=5))['decision']=='LOCAL_COMMITTED_PREAUTHORIZED'
    before=external_head(lab)
    with sqlite3.connect(lab['db']) as con:
        con.execute('DELETE FROM g1_local_commits')
        hacked=dict(local_head(lab),state_digest=business_digest(con))
        con.execute('UPDATE g1_e8_head SET head_json=?',(json.dumps(hacked),))
    out=submit(lab,request('r_hacked','tx_hacked',epoch=5))
    assert out=={'decision':'HARD_HOLD','reason':'E8_HEAD_DIVERGENCE'}
    assert external_head(lab)==before
