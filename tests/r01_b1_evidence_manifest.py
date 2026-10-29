"""Package independent R-01 raw observations with per-file SHA-256.

Generate only after tests; exit nonzero if any required adversarial vector
was not observed or a same-UID bypass is relabelled PASS.
"""
from __future__ import annotations
import argparse,hashlib,json,os,platform,subprocess,sys
from pathlib import Path

REQUIRED={
    "R01-N01":"PASS","R01-N02":"PASS","R01-N03":"FAIL",
    "R01-N04":"FAIL","R01-N05":"FAIL","R01-N06":"FAIL",
    "B1-SAME-UID-COUNTEREXAMPLE":"FAIL","B1-WRITER-HEALTH":"PASS",
    **{"B1-OS-"+s:"PASS" for s in (
        "ordinary-budget","ordinary-outbox","spoofed-udf",
        "drop-trigger","replace","direct-sql","direct-native",
        "modify-source","modify-marker","modify-witness",
    )},
}
BASELINE="9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--directory",required=True)
    p=Path(parser.parse_args().directory)
    results={}
    for key in REQUIRED:
        path=p/(key+".json")
        if not path.is_file():
            raise SystemExit("MISSING R01 VECTOR: "+key)
        obj=json.loads(path.read_text(encoding="utf-8"))
        if obj["sut_sha"]!=BASELINE or obj["security_verdict"]!=REQUIRED[key]:
            raise SystemExit("INVALID R01 VECTOR STATUS: "+key)
        results[key]=obj["security_verdict"]
    digest={
        "schema":"tenir.r01.audit-evidence.v1",
        "baseline_sha":BASELINE,
        "execution_head":subprocess.check_output(
            ["git","rev-parse","HEAD"],text=True).strip(),
        "python":sys.version,"sqlite":__import__("sqlite3").sqlite_version,
        "platform":platform.platform(),"controller_euid":os.geteuid(),
        "writer_uid":61021,"untrusted_uid":61022,
        "scope":"disposable POSIX single-host; no actual service deployment",
        "status":"B1_POSIX_DAC_LAB_PASS_SAME_UID_PRIVILEGED_EXCLUSIVITY_FAIL",
        "source_lock_only":True,
        "production_integration_lock":False,
        "vectors":results,
        "limits":[
            "Unpatched same-UID code can forge SQLite UDF, drop triggers or replace DB",
            "Linux DAC proof assumes isolated EUID and owner-only directories",
            "Privileged root/CAP_DAC_OVERRIDE and multi-host environments not fenced",
            "No real provider dispatch or independently deployed writer service qualified",
        ],
        "sha256":{
            f.name:{"sha256":sha(f),"size":f.stat().st_size}
            for f in sorted(p.iterdir())
            if f.is_file() and f.name!="r01-bundle-manifest.json"
        },
    }
    (p/"r01-bundle-manifest.json").write_text(
        json.dumps(digest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({
        "baseline":BASELINE,"head":digest["execution_head"],
        "vectors":len(results),"verdict":digest["status"],
    },sort_keys=True))


if __name__=="__main__":
    main()
