import base64
import os
import sqlite3
import uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from g1_ipc import canonical, initialize, process, validate

KEY = Ed25519PrivateKey.generate()
UID = os.getuid()

def request(reqid='r1', tx='tx1', epoch=1):
    data = {'header': {'protocol_version':'g1-ipc-v0.1','request_id':reqid,'timestamp':1777218342,'realm':'test','epoch':epoch,'operation':'COMMIT'}, 'auth':{'worker_uid':UID,'grant_signature':'NOT_IMPLEMENTED'}, 'payload':{'transaction_id':tx,'state_transition':{'from':'PENDING','to':'COMMITTED'},'payload_digest':'sha256:'+'0'*64}}
    data['envelope_signature'] = 'ed25519:' + base64.b64encode(KEY.sign(b'TENIR-G1-IPC-ENVELOPE-v0.1\x00'+canonical(data))).decode()
    return data

def test_commit_replay_and_conflict(tmp_path):
    db=str(tmp_path/'g1.db'); initialize(db)
    keys={UID:KEY.public_key()}
    assert process(db,request(),UID,keys)['decision']=='COMMITTED'
    assert process(db,request(),UID,keys)['replayed'] is True
    assert process(db,request('r2'),UID,keys)['decision']=='CONFLICT'
    assert process(db,request('r3','tx1',epoch=2),UID,keys)['decision']=='CONFLICT'
    assert sqlite3.connect(db).execute('select count(*) from g1_local_commits').fetchone()[0]==1

def test_peer_and_signature_rejected(tmp_path):
    db=str(tmp_path/'g1.db'); initialize(db); keys={UID:KEY.public_key()}
    import pytest
    with pytest.raises(ValueError,match='peer UID mismatch'):
        process(db,request(),UID+1,keys)
    bad=request();bad['payload']['transaction_id']='forged'
    with pytest.raises(ValueError,match='signature'):
        process(db,bad,UID,keys)
    assert sqlite3.connect(db).execute('select count(*) from g1_local_commits').fetchone()[0]==0

def test_same_id_different_payload_conflicts(tmp_path):
    db=str(tmp_path/'g1.db'); initialize(db);keys={UID:KEY.public_key()}
    assert process(db,request(),UID,keys)['decision']=='COMMITTED'
    assert process(db,request('r1','different'),UID,keys)['decision']=='CONFLICT'
