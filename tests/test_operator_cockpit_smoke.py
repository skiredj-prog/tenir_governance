"""Local operator-cockpit smoke test for the hardened R5 server.

Covers the unauthenticated health check, a 401 on /api/v1/state without a
bearer token, a 200 on that route with the token, and the cockpit page
served from the API origin. The extra cases lock the shadow, ledger,
ceremony, and ENFORCE contracts the page depends on.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

TOKEN = "cockpit-smoke-token-0123456789abcdef"
OPERATOR = "operator-1"
SECRET = "cockpit-smoke-oath-secret-0123456789"

_LEDGER_DIR = tempfile.mkdtemp(prefix="tenir-cockpit-smoke-")
os.environ["TENIR_API_TOKEN"] = TOKEN
os.environ["TENIR_OPERATOR_ID"] = OPERATOR
os.environ["OATH_SECRET"] = SECRET
os.environ["TENIR_MODE"] = "SHADOW_PASSIVE"
os.environ["LEDGER_PATH"] = str(Path(_LEDGER_DIR) / "ledger.jsonl")
os.environ["NEO4J_PASSWORD"] = ""
os.environ["OLLAMA_MODEL"] = "grammar"
os.environ["TENIR_CORS_ORIGINS"] = ""

_R5_ROOT = Path(__file__).resolve().parents[1] / "r5_hardened" / "IRON_OMEGA_R5"
sys.path.insert(0, str(_R5_ROOT))
try:
    from r5_hardened.IRON_OMEGA_R5 import r5_server
finally:
    try:
        sys.path.remove(str(_R5_ROOT))
    except ValueError:
        pass

from fastapi.testclient import TestClient


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _runtime_object() -> dict:
    path = Path(__file__).resolve().parents[1] / "tenir_kernel" / "examples" / "quickstart_runtime_object.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scores"]["pressure"] = 0.5
    payload["scores"]["velocity"] = 0.5
    payload["scores"]["capacity"] = 0.8
    payload["scores"]["option_space"] = 0.75
    payload["identity"]["unit_name"] = OPERATOR
    payload["trace"]["trace_id"] = "cockpit-smoke"
    payload["timestamp_utc"] = "2026-11-25T00:00:00Z"
    return payload


def _ws_protocol(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
    return f"bearer.b64url.{encoded}"


@pytest.fixture(scope="module")
def client():
    with TestClient(r5_server.app) as test_client:
        yield test_client


def test_health_is_open(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_state_requires_bearer_token(client):
    missing = client.get("/api/v1/state")
    assert missing.status_code == 401
    wrong = client.get("/api/v1/state", headers={"Authorization": "Bearer not-the-token"})
    assert wrong.status_code == 401
    assert TOKEN not in missing.text


def test_state_with_token(client):
    response = client.get("/api/v1/state", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "SHADOW_PASSIVE"
    assert "entry_count" in body


def test_cockpit_page_is_served_from_the_api_origin(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    page = response.text
    assert "Cockpit" in page
    assert "Forensics" in page
    assert "Ceremony" in page
    assert "TENIR_API_TOKEN" in page
    assert "TENIR_OPERATOR_ID" in page
    assert "runtime_object" in page
    assert "http://localhost:8000" not in page
    assert "innerHTML" not in page
    assert "insertAdjacentHTML" not in page
    policy = response.headers["content-security-policy"]
    assert "script-src 'nonce-" in policy
    assert "unsafe-eval" not in policy
    assert "connect-src 'self'" in policy
    assert "http://127.0.0.1:*" in policy
    nonce = policy.split("nonce-", 1)[1].split("'", 1)[0]
    assert f'nonce="{nonce}"' in page
    remembered = page.split("function rememberSession()", 1)[1].split("function loadSession()", 1)[0]
    assert "jwtInp" not in remembered
    alias = client.get("/cockpit")
    assert alias.status_code == 200
    assert "Operator Cockpit" in alias.text or "Cockpit" in alias.text


def test_shadow_passive_adjudication_ledger_and_ceremony(client):
    adjudicated = client.post(
        "/api/v1/adjudicate",
        headers=_auth(),
        json={"runtime_object": _runtime_object(), "actor_ref": OPERATOR, "case_id": "cockpit-smoke"},
    )
    assert adjudicated.status_code == 200, adjudicated.text
    verdict = adjudicated.json()
    assert verdict["operating_mode"] == "SHADOW_PASSIVE"
    assert verdict["schema_version"] == "1.2"
    assert verdict["entry_id"]

    narrative = client.post(
        "/api/v1/adjudicate",
        headers=_auth(),
        json={"raw_input": "query the research project status"},
    )
    assert narrative.status_code == 200, narrative.text

    recent = client.get("/api/v1/ledger/recent?n=10", headers=_auth())
    assert recent.status_code == 200
    entries = recent.json()
    assert isinstance(entries, list) and entries
    assert entries[-1]["entry_hash"]

    verified = client.get("/api/v1/ledger/verify", headers=_auth())
    assert verified.status_code == 200
    report = verified.json()
    assert report["valid"] is True
    assert report["entry_count"] >= 1

    rejected = client.post(
        "/api/v1/oath/sign",
        headers=_auth(),
        json={"operator_id": "someone-else"},
    )
    assert rejected.status_code == 403

    signed = client.post(
        "/api/v1/oath/sign",
        headers=_auth(),
        json={"operator_id": OPERATOR},
    )
    assert signed.status_code == 200, signed.text
    oath = signed.json()
    assert oath["nonce"] and oath["timestamp"] and oath["signature"]

    transition = client.post(
        "/api/v1/transition",
        headers=_auth(),
        json={
            "operator_id": OPERATOR,
            "target_mode": "SHADOW_CRITICAL",
            "oath_text": oath["oath_text"],
            "oath_signature": oath["signature"],
            "nonce": oath["nonce"],
            "timestamp": oath["timestamp"],
        },
    )
    assert transition.status_code == 200, transition.text
    assert transition.json()["to_mode"] == "SHADOW_CRITICAL"
    assert client.get("/api/v1/state", headers=_auth()).json()["mode"] == "SHADOW_CRITICAL"


def test_enforce_rejects_raw_input_and_accepts_runtime_object(client):
    previous = r5_server.app.state.mode
    r5_server.app.state.mode = "ENFORCE"
    try:
        rejected = client.post(
            "/api/v1/adjudicate",
            headers=_auth(),
            json={"raw_input": "query the research project status"},
        )
        assert rejected.status_code == 422
        assert "runtime_object" in rejected.text
        accepted = client.post(
            "/api/v1/adjudicate",
            headers=_auth(),
            json={"runtime_object": _runtime_object(), "actor_ref": OPERATOR},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["operating_mode"] == "ENFORCE"
        assert accepted.json()["schema_version"] == "1.2"
    finally:
        r5_server.app.state.mode = previous


def test_websocket_requires_the_bearer_subprotocol(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/vps") as socket:
            socket.receive_json()

    with client.websocket_connect("/ws/vps", subprotocols=[_ws_protocol(TOKEN)]) as socket:
        frame = socket.receive_json()
    assert frame["type"] == "CONNECTED"
