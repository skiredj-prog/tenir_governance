# RV-003 T1-P / T2-P — Independent re-verification procedure

## Scope
Offline lab campaign only. No native TENIR-Gov changes. No live Magertron.

## Prerequisites
- Python 3.10+
- The archive unpacked
- Native checkout at SHA 674c8606089251ea2f74890bd47bce3a717c3b91 (read-only)

## Steps
1. Verify manifest:
   ```
   cd rv003_t1t2_archive
   sha256sum -c MANIFEST.sha256
   ```
2. Inspect results:
   ```
   python3 -c "import json; print(json.load(open('results/t1_t2_results.json')))"
   ```
3. Independent SQLite readback (example for T2-P):
   ```
   python3 - << 'PY'
   import sqlite3
   con = sqlite3.connect("stores/t2p/receiver.db")
   print("ops:", list(con.execute("SELECT operation_id, state FROM ops")))
   print("effects:", list(con.execute("SELECT operation_id FROM effects")))
   print("consumptions:", list(con.execute("SELECT operation_id FROM consumptions")))
   print("receipts:", list(con.execute("SELECT kind, decision, COUNT(*) FROM receipts GROUP BY kind, decision")))
   PY
   ```
4. Confirm causal order in receipts (acceptance seq < commit seq for T2-P).
5. Confirm execute_calls == 0 for T1-P and execute_calls == 1 for T2-P in results.
6. Confirm native working tree remains clean at the pinned SHA.

## Expected observations
- T1-P: DENY_REVOKED before dispatch, 0 execute, receiver NEW, 0 effect.
- T2-P: ACCEPTED_PAUSED, then lab permission revoked, then DENIED_REVOKED, 0 effect, 0 consumption.
