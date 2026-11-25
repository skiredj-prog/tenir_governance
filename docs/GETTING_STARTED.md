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

Open http://127.0.0.1:8000/ . Paste `TENIR_API_TOKEN` into Bearer and `TENIR_OPERATOR_ID` into Operator, then Apply. Leave API and WS empty to use this origin. The operator id and the two base URLs stay in `sessionStorage` for the tab. The API token does not: it lives only in page memory and must be pasted again after every reload. Do not put the token in the URL.

The ceremony is a limited local demo. It checks that shared `TENIR_API_TOKEN` and the single configured `TENIR_OPERATOR_ID`. It does not prove an independent personal identity, and it is not an account system. `ENFORCE` on this server is the R5 process mode (`app.state.mode` in `r5_hardened/IRON_OMEGA_R5/r5_server.py`, advanced only after `ledger.append_control_transition`). That handler does not call `DualKernelGovernor`, the operation registry, or `tenir_governance.sdk`. A mode change here grants no admission, dispatch, budget, or attestation authority on the 5.7.x governance kernel.

An oath is fresh only when its timestamp is an aware UTC instant and `now - signed_at` is between `-TENIR_OATH_FUTURE_SKEW_SECONDS` and `TENIR_OATH_MAX_AGE_SECONDS`, both edges included. The defaults are 30 seconds of future skew and 300 seconds of age. A missing, naive, or unparseable timestamp is rejected. The nonce is recorded only after the HMAC and the freshness check both succeed, so a stale oath does not burn the nonce. `TENIR_OATH_MAX_AGE_SECONDS` must be a finite number greater than 0. `TENIR_OATH_FUTURE_SKEW_SECONDS` must be a finite number greater than or equal to 0. Any other value refuses to start.

## Operator cockpit — manual checks

Run the server with the command above. Use a fresh ledger. Expected results below are what the page shows, not a certification of the kernel.

1. Open http://127.0.0.1:8000/ with the token field empty. The hint asks for the API token. The WebSocket label stays `token required`. Reload: the token field is still empty. The operator id may still be filled if you saved it earlier.
2. Paste a wrong bearer value and the real `TENIR_OPERATOR_ID`, then Apply. The hint shows `401`. No cockpit data is trusted from that call.
3. Paste the real token and operator id, then Apply. The hint shows `Connected · SHADOW_PASSIVE · loopback only`. The WebSocket label shows `127.0.0.1:8000/ws/vps` (or the port you chose).
4. Set API to `https://example.com` and Apply. The hint says the base must be localhost or 127.0.0.1. Clear API and Apply again. The hint returns to Connected.
5. Cockpit: press Adjudicate. A badge appears (`allow` at the default sliders), with `S=`, `REST`, and `SHADOW_PASSIVE`. The trajectory gains a point.
6. Forensics: Load entries, then Verify chain. One observation row appears. The chain badge says `Chain valid`.
7. Ceremony: set the operator id to `someone-else` in the session field, Apply, open Ceremony, Request oath. The red box shows `403` and says the operator id must match `TENIR_OPERATOR_ID`. Put the real operator id back and Apply.
8. Request oath as the configured operator. The oath text, signature, nonce, and timestamp fill in. Set the target to `ENFORCE` and Submit. The red box shows `409` (ENFORCE needs `SHADOW_CRITICAL` first).
9. Request a new oath, set the target to `SHADOW_CRITICAL`, Submit. The green box names `SHADOW_CRITICAL`. Request another oath, set the target to `ENFORCE`, Submit. The green box names `ENFORCE` and says this R5 mode does not grant authority on the 5.7.x governance kernel. Cockpit Adjudicate still returns a verdict, now under `ENFORCE`.
10. In the browser console, `buildRuntimeObject` is the normal path. A hand-built `fetch` of `/api/v1/adjudicate` with `{ "raw_input": "query the research project status" }` while the mode is `ENFORCE` returns `422` and names `runtime_object`. The page itself always sends a schema 1.2 object, so its Adjudicate button does not take that path.

Browser coverage of the same steps, including a ledger and WebSocket payload that contains `<script>` and `<img onerror>`, is `tests/test_operator_cockpit_browser.py`. Locally it uses system Chrome when that binary exists, and skips when it does not. The pull-request job `Operator cockpit browser (Playwright + Chromium)` checks out the exact PR head, installs Playwright Chromium, sets `TENIR_REQUIRE_BROWSER=1`, and fails if either case is skipped. The API smoke test does not need a browser:

```sh
python -m pytest tests/test_operator_cockpit_smoke.py tests/test_operator_cockpit_browser.py -q
```

`TENIR_POLICY_PROFILE` defaults to `default`, which is `PolicyEngine.default()`. On kernel 5.7.6 the server accepts only `default` and `canonical`. The older names `um6p_shadow_v4` and `ocp_sovereign_pilot` are not factories in this package; any other name refuses to start.

`docker compose up -d --build` is the same server. Copy `.env.example` to `.env`, replace the three secrets, and keep the published port on `127.0.0.1`. The container listens on all of its own interfaces so Docker can publish that loopback port. Neo4j is optional; without it the graph features stay disabled and the cockpit still runs.

What the page does against kernel 5.7.6:

- Cockpit submits a canonical TENIR 2.0 schema 1.2 `runtime_object` built from the sliders. `SHADOW_PASSIVE` records it. `ENFORCE` rejects `raw_input` and accepts only that schema. `SHADOW_OFF` does not record the submission.
- Forensics calls `GET /api/v1/ledger/recent?n=100` and `GET /api/v1/ledger/verify`.
- Ceremony posts `{"operator_id": ...}` to `/api/v1/oath/sign` and submits the returned signature, nonce, and timestamp unchanged. The operator id must match `TENIR_OPERATOR_ID`. That pair is a shared local secret plus one configured id. It is not a personal identity. `ENFORCE` is legal only after a recorded `SHADOW_CRITICAL` transition; otherwise the server returns 409 and the page shows that reason. The resulting mode is local to this R5 process.
- The live stream uses the WebSocket subprotocol `bearer.b64url.<token>` because a browser cannot set `Authorization` on WebSocket. The server still compares the decoded value with `TENIR_API_TOKEN`.

Check the wiring without a browser:

```sh
python -m pytest tests/test_operator_cockpit_smoke.py -q
```

The smoke test covers `/health`, a 401 on `/api/v1/state` without a bearer token, a 200 on that route with the token, and the cockpit HTML being served by the API. It also exercises shadow adjudication, the ledger, the oath, and the `ENFORCE` `raw_input` rejection. It needs the `r5` extra installed above. The kernel suite does not.
