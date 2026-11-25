# TENIR-Gov developer quickstart

The quickstart ships with TENIR-Gov 5.7.6 (`tenir_governance/quickstart.py`, present since the unpublished 5.7.5 candidate); the 5.7.0 package predates it. The setup below installs from a checkout containing this guide.

## Setup

Use Python 3.10 or later, preferably in a virtual environment. From the repository root:

```sh
python -m pip install -e .
python -m tenir_governance.quickstart preflight
```

## See the benefit within five minutes after setup

```sh
python -m tenir_governance.quickstart run --output booking-demo
python -m tenir_governance.quickstart verify booking-demo
```

Use a fresh output directory for each run. The simulated booking is created and its response is lost. TENIR-Gov preserves UNKNOWN and refuses a repeat dispatch. One synthetic booking exists. A status lookup returns a correlated receipt and resolves the outcome.

To see how the operation stays UNKNOWN when the receipt is unavailable, run:

```sh
python -m tenir_governance.quickstart run --keep-unknown --output booking-unknown
python -m tenir_governance.quickstart verify booking-unknown
```

Open `booking-demo/evidence.json` or `booking-unknown/evidence.json` for the admission, operation state, dispatch count, budget and receipt. Verification checks local consistency and file hashes. The files contain unsigned synthetic evidence. Five minutes is the onboarding target, measured from preflight through evidence inspection.

For your own sandbox, start with the `SyntheticHotel` class in [quickstart.py](../tenir_governance/quickstart.py) and the [provider interface](../tenir_governance/operations/provider.py). Decide how the provider lets you look up the original effect and how it distinguishes uncertainty from confirmed non-execution before configuring dispatch.

## Operator cockpit (local only)

The operator cockpit, forensics console, and authority ceremony are one page, [interface/TENIR_Interface_2cIronOmega_v1.html](../interface/TENIR_Interface_2cIronOmega_v1.html). The hardened server serves that page and the API on the same origin. Do not open the file directly, and do not publish it. The browser refuses any API or WebSocket base that is not `localhost`, `127.0.0.1`, or `::1`.

From the repository root, with Python 3.10 or later:

```sh
python -m pip install -e '.[r5]'
export TENIR_API_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export TENIR_OPERATOR_ID="operator-1"
export OATH_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export TENIR_MODE="SHADOW_PASSIVE"
export TENIR_POLICY_PROFILE="default"
export LEDGER_PATH="$PWD/ledger/tenir_ledger.jsonl"
export PYTHONPATH="r5_hardened/IRON_OMEGA_R5${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p ledger
uvicorn r5_hardened.IRON_OMEGA_R5.r5_server:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/ . Paste `TENIR_API_TOKEN` into Bearer and `TENIR_OPERATOR_ID` into Operator, then Apply. Leave API and WS empty to use this origin. A saved session stays in `sessionStorage` for the tab. Do not put the token in the URL.

`TENIR_POLICY_PROFILE` defaults to `default`, which is `PolicyEngine.default()`. On kernel 5.7.6 the server accepts only `default` and `canonical`. The older names `um6p_shadow_v4` and `ocp_sovereign_pilot` are not factories in this package; any other name refuses to start.

`docker compose up -d --build` is the same server. Copy `.env.example` to `.env`, replace the three secrets, and keep the published port on `127.0.0.1`. The container listens on all of its own interfaces so Docker can publish that loopback port. Neo4j is optional; without it the graph features stay disabled and the cockpit still runs.

What the page does against kernel 5.7.6:

- Cockpit submits a canonical TENIR 2.0 schema 1.2 `runtime_object` built from the sliders. `SHADOW_PASSIVE` records it. `ENFORCE` rejects `raw_input` and accepts only that schema. `SHADOW_OFF` does not record the submission.
- Forensics calls `GET /api/v1/ledger/recent?n=100` and `GET /api/v1/ledger/verify`.
- Ceremony posts `{"operator_id": ...}` to `/api/v1/oath/sign` and submits the returned signature, nonce, and timestamp unchanged. The operator id must match `TENIR_OPERATOR_ID`. `ENFORCE` is legal only after a recorded `SHADOW_CRITICAL` transition; otherwise the server returns 409 and the page shows that reason.
- The live stream uses the WebSocket subprotocol `bearer.b64url.<token>` because a browser cannot set `Authorization` on WebSocket. The server still compares the decoded value with `TENIR_API_TOKEN`.

Check the wiring without a browser:

```sh
python -m pytest tests/test_operator_cockpit_smoke.py -q
```

The smoke test covers `/health`, a 401 on `/api/v1/state` without a bearer token, a 200 on that route with the token, and the cockpit HTML being served by the API. It also exercises shadow adjudication, the ledger, the oath, and the `ENFORCE` `raw_input` rejection. It needs the `r5` extra installed above. The kernel suite does not.
