from __future__ import annotations
import hashlib, json, os, shutil, sqlite3, stat
from pathlib import Path
import pytest
from test_g4a_atomic_simulated_settlement import lab, admit, open_authority


def dbpath(backend):
    return Path(backend._database_path)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def rows(backend):
    with backend._connection() as db:
        return {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')]
                for t in ('resources','reservations','g4a_current','g4a_outbox','g4a_commands')}

def test_R01_N01_ordinary_update_refused(lab):
    _, b, a = lab; admit(a); p=dbpath(b); before=digest(p); snap=rows(b)
    with sqlite3.connect(p) as db:
        with pytest.raises(sqlite3.DatabaseError): db.execute("UPDATE resources SET reserved='19'")
    assert digest(p)==before and rows(b)==snap

def test_R01_N02_ordinary_outbox_current_insert_update_refused(lab):
    _, b, a=lab; admit(a); p=dbpath(b); before=digest(p); snap=rows(b)
    with sqlite3.connect(p) as db:
        for sql in ("UPDATE g4a_current SET operation_state='SIMULATED_CONFIRMED'", "INSERT INTO g4a_outbox SELECT operation_key,version+1,event_hash,event_json FROM g4a_current"):
            with pytest.raises(sqlite3.DatabaseError): db.execute(sql)
    assert digest(p)==before and rows(b)==snap

def test_R01_N03_spoofed_udf_privileged_budget_mutation_commits(lab):
    _, b, a=lab; admit(a); p=dbpath(b); before=rows(b)
    with sqlite3.connect(p) as db:
        db.create_function('tenir_g4a_write',0,lambda:1)
        db.execute("UPDATE resources SET reserved='19'"); db.commit()
    after=rows(b); assert after!=before
    with pytest.raises(Exception): open_authority(b._store_dir)
    print(json.dumps({'counterexample':'UPDATE committed using spoofed UDF', 'restart':'rejected', 'reserved_before':before['resources'], 'reserved_after':after['resources']}))

def test_R01_N04_privileged_drop_trigger_then_mutation_commits(lab):
    _, b, a=lab; admit(a); p=dbpath(b); before=rows(b)
    with sqlite3.connect(p) as db:
        db.create_function('tenir_g4a_write',0,lambda:1)
        db.execute('DROP TRIGGER g4a_guard_resources_update')
        db.execute("UPDATE resources SET reserved='19'"); db.commit()
    after=rows(b); assert after!=before
    with pytest.raises(Exception): open_authority(b._store_dir)
    print(json.dumps({'counterexample':'DROP TRIGGER + UPDATE committed', 'restart':'rejected'}))

def test_R01_N05_same_uid_database_file_replacement_succeeds(lab, tmp_path):
    _, b, a=lab; admit(a); p=dbpath(b); old=digest(p)
    replacement=tmp_path/'replacement.sqlite3'; shutil.copy2(p,replacement)
    assert os.stat(replacement).st_uid==os.getuid()
    os.replace(replacement,p)
    assert digest(p)==old
    assert open_authority(b._store_dir).inspect('lab:g4a').operation_state=='RESERVED'
    print(json.dumps({'counterexample':'same-UID os.replace DB copy succeeded', 'uid':os.getuid(), 'reopen':'accepted identical copy'}))

def test_R01_N06_independent_writer_inventory(lab):
    _, backend, _ = lab
    root=Path(__file__).resolve().parents[1]/'tenir_governance'/'operations'
    files=list(root.rglob('*.py'))
    hits=[]
    for f in files:
        text=f.read_text(errors='replace')
        if any(s in text for s in ('sqlite3.connect(', '._connection()', '.execute("INSERT', '.execute("UPDATE', '.execute("DELETE')):
            hits.append(str(f.relative_to(root.parent.parent)))
    declared=list((root.parent.parent/'docs'/'g4a').glob('*writer*')) if (root.parent.parent/'docs'/'g4a').exists() else []
    assert hits
    db=Path(backend._database_path); st=db.stat(); dst=Path(backend._store_dir).stat()
    print(json.dumps({'independent_source_scan_files':hits,'declared_writer_inventory_artifacts':[str(p) for p in declared], 'runtime_process':{'pid':os.getpid(),'uid':os.getuid(),'gid':os.getgid()}, 'database':{'path':str(db),'uid':st.st_uid,'gid':st.st_gid,'mode':oct(stat.S_IMODE(st.st_mode))}, 'database_directory':{'uid':dst.st_uid,'gid':dst.st_gid,'mode':oct(stat.S_IMODE(dst.st_mode))}, 'scope':'static source scan only; cannot prove runtime exhaustiveness'}))
