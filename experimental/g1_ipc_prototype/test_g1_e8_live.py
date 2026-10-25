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
from test_g1_ipc import KEY, UID, request


@pytest.fixture
def lab(tmp_path):
    engine_key = Ed25519PrivateKey.generate()
    witness_key = Ed25519PrivateKey.generate()
    store = WitnessStore(str(tmp_path/'witness.sqlite'), witness_key, engine_key.public_key())
    initial = store.provision_for_test('test', 'lab-store', epoch=5)
    db = str(tmp_path/'engine.sqlite')
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
    assert out['reason']=='E8_HEAD_DIVERGENCE'
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
