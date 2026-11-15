"""Independent restart probe: reloads receiver against a persisted store and attempts RELEASE.
Usage: python restart_replay_probe.py <run_dir> <operation_id>
"""
import json, os, sys, sqlite3
from pathlib import Path

RUN = Path(sys.argv[1])
OP = sys.argv[2]
os.environ["RUN"] = str(RUN)
os.environ["RUN_ID"] = "restart-probe"
os.environ["TENIR_GOV_CHECKOUT"] = os.environ.get("TENIR_GOV_CHECKOUT", "/tmp/tenir_gov_base")
sys.path.insert(0, "/tmp/ep02_inspect/TRACKP_EP02_ALIGN_RESULTS/source")

import receiver_p as R
R.RUN = str(RUN)
R.RUN_ID = "restart-probe"
# Do not call init() — store already exists

resp = R.control({"cmd": "RELEASE", "operation_id": OP})
con = sqlite3.connect(RUN / "state/receiver.db")
effects = con.execute("SELECT COUNT(*) FROM effects WHERE operation_id=?", (OP,)).fetchone()[0]
state = con.execute("SELECT state FROM ops WHERE operation_id=?", (OP,)).fetchone()[0]
print(json.dumps({
    "restarted": True,
    "replay_response": str(resp)[:300],
    "effects_after_restart_replay": effects,
    "state_after": state,
    "no_new_effect": effects == 0,
}))
