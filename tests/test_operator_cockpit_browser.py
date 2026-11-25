"""Headless Chrome coverage for the local operator cockpit.

Skips when Playwright or a Chrome binary is missing, so the kernel suite
stays green without a browser. The security case feeds ledger, WebSocket,
and error bodies that contain markup and checks that the page shows them
as text.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

TOKEN = "cockpit-browser-token-0123456789abcdef"
OPERATOR = "operator-1"
SECRET = "cockpit-browser-oath-secret-0123456789"
XSS = "<script>window.__xss=1</script><img src=x onerror=\"window.__xss=2\">\"'><svg/onload=window.__xss=3>"
REPO = Path(__file__).resolve().parents[1]
R5_ROOT = REPO / "r5_hardened" / "IRON_OMEGA_R5"
ARTIFACTS = Path("/opt/cursor/artifacts")
CHROME = next(
    (
        candidate
        for candidate in (
            os.environ.get("CHROME_PATH", ""),
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            shutil.which("google-chrome") or "",
        )
        if candidate and Path(candidate).exists()
    ),
    "",
)

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome is not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def cockpit_origin(tmp_path_factory):
    port = _free_port()
    ledger = tmp_path_factory.mktemp("ledger") / "ledger.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "TENIR_API_TOKEN": TOKEN,
            "TENIR_OPERATOR_ID": OPERATOR,
            "OATH_SECRET": SECRET,
            "TENIR_MODE": "SHADOW_PASSIVE",
            "TENIR_POLICY_PROFILE": "default",
            "LEDGER_PATH": str(ledger),
            "OLLAMA_MODEL": "grammar",
            "NEO4J_PASSWORD": "",
            "PYTHONPATH": str(R5_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "r5_hardened.IRON_OMEGA_R5.r5_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"cockpit server exited early:\n{output}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                ready = True
                break
        except OSError:
            time.sleep(0.1)
    if not ready:
        process.kill()
        raise RuntimeError("cockpit server did not accept connections")
    try:
        yield origin
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(
            executable_path=CHROME,
            args=["--no-sandbox", "--disable-gpu"],
        )
        try:
            yield launched
        finally:
            launched.close()


def _shot(page, name: str) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(ARTIFACTS / name), full_page=True)


def _connect(page, token=TOKEN, operator=OPERATOR):
    page.fill("#jwtInp", token)
    page.fill("#opSession", operator)
    page.fill("#apiBase", "")
    page.fill("#wsBase", "")
    page.click("#btnApplyConn")


def _assert_inert(page, root: str) -> None:
    assert page.locator(f"{root} script").count() == 0
    assert page.locator(f"{root} img").count() == 0
    assert page.locator(f"{root} svg").count() == 0
    assert page.evaluate("() => window.__xss") is None


def test_three_surfaces_errors_and_reload(cockpit_origin, browser):
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page.goto(cockpit_origin + "/", wait_until="domcontentloaded")
    nonce = page.evaluate("() => document.querySelector('script').nonce")
    assert nonce
    response_policy = page.request.get(cockpit_origin + "/").headers["content-security-policy"]
    assert "script-src 'nonce-" in response_policy
    assert "unsafe-eval" not in response_policy

    page.evaluate("() => sessionStorage.setItem('tenir.cockpit.token', 'leftover-secret')")
    page.reload(wait_until="domcontentloaded")
    assert page.evaluate("() => sessionStorage.getItem('tenir.cockpit.token')") is None
    assert page.locator("#jwtInp").input_value() == ""
    assert "API token" in page.locator("#connHint").inner_text()

    _connect(page, token="wrong-token-wrong-token-wrong-token")
    page.wait_for_function("() => document.getElementById('connHint').textContent.includes('401')")

    _connect(page)
    page.wait_for_function(
        "() => document.getElementById('connHint').textContent.startsWith('Connected')"
    )
    assert "SHADOW_PASSIVE" in page.locator("#connHint").inner_text()
    _shot(page, "pr148_cockpit_connected.png")

    page.evaluate(
        """() => {
          window.__build = buildRuntimeObject;
          buildRuntimeObject = () => ({nope: true, trace: {trace_id: 'bad-object'}});
        }"""
    )
    page.click("#adjBtn")
    page.wait_for_function("() => document.getElementById('adjDetail').innerText.includes('422')")
    detail = page.locator("#adjDetail").inner_text()
    assert "runtime_object" in detail
    assert page.locator("#adjDetail script").count() == 0
    page.evaluate("() => { buildRuntimeObject = window.__build; }")
    _shot(page, "pr148_error_422.png")

    page.click("#adjBtn")
    page.wait_for_function("() => document.getElementById('adjBadge').textContent.length > 0")
    assert "SHADOW_PASSIVE" in page.locator("#adjDetail").inner_text()

    page.click(".tab[data-tab='forensics']")
    page.click("#btnLoad")
    page.wait_for_selector("#timeline .tl-row")
    page.click("#btnVerify")
    page.wait_for_function("() => document.getElementById('chainLbl').textContent.includes('Chain valid')")
    _shot(page, "pr148_forensics.png")

    _connect(page, operator="someone-else")
    page.click(".tab[data-tab='ceremony']")
    page.click("#btnOath")
    page.wait_for_function("() => document.getElementById('oathRes').innerText.includes('403')")
    assert "TENIR_OPERATOR_ID" in page.locator("#oathRes").inner_text()
    _shot(page, "pr148_ceremony_403.png")

    _connect(page, operator=OPERATOR)
    page.click(".tab[data-tab='ceremony']")
    page.evaluate("() => { document.getElementById('tNonce').value = ''; }")
    page.click("#btnOath")
    page.wait_for_function("() => document.getElementById('oathRes').innerText.includes('Oath signed')")
    page.select_option("#tMode", "ENFORCE")
    page.click("#btnTrans")
    page.wait_for_function("() => document.getElementById('transRes').innerText.includes('409')")
    _shot(page, "pr148_ceremony_409.png")

    page.evaluate("() => { document.getElementById('tNonce').value = ''; }")
    page.click("#btnOath")
    page.wait_for_function("() => document.getElementById('tNonce').value.length > 0")
    page.select_option("#tMode", "SHADOW_CRITICAL")
    page.click("#btnTrans")
    page.wait_for_function("() => document.getElementById('transRes').innerText.includes('SHADOW_CRITICAL')")
    page.evaluate("() => { document.getElementById('tNonce').value = ''; }")
    page.click("#btnOath")
    page.wait_for_function("() => document.getElementById('tNonce').value.length > 0")
    page.select_option("#tMode", "ENFORCE")
    page.click("#btnTrans")
    page.wait_for_function("() => document.getElementById('transRes').innerText.includes('ENFORCE')")
    assert "5.7.x" in page.locator("#transRes").inner_text()
    page.click(".tab[data-tab='cockpit']")
    page.click("#adjBtn")
    page.wait_for_function("() => document.getElementById('pbMode').textContent === 'ENFORCE'")
    _shot(page, "pr148_cockpit_enforce.png")

    page.reload(wait_until="domcontentloaded")
    assert page.locator("#jwtInp").input_value() == ""
    assert page.locator("#opSession").input_value() == OPERATOR
    assert page.evaluate("() => sessionStorage.getItem('tenir.cockpit.token')") is None
    assert "API token" in page.locator("#connHint").inner_text()
    _shot(page, "pr148_reload_token_cleared.png")
    page.close()


def test_untrusted_ledger_ws_and_error_stay_text(cockpit_origin, browser):
    payload = [{
        "entry_type": XSS,
        "ces_state": XSS,
        "membrane_decision": XSS,
        "override_operator": XSS,
        "entry_hash": XSS,
        "previous_hash": XSS,
        "rationale": XSS,
        "timestamp": "2026-11-25T00:00:00Z",
    }]
    page = browser.new_page(viewport={"width": 1440, "height": 1100})

    def recent(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def adjudicate(route):
        route.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": XSS}))

    def socket(ws):
        ws.send(json.dumps({
            "type": "TRAJECTORY_UPDATE",
            "payload": {
                "s_score": 1.25,
                "ds_de": 0.0,
                "d2s_de2": 0.0,
                "ces_state": XSS,
                "membrane_decision": XSS,
                "operating_mode": "SHADOW_PASSIVE",
            },
        }))

    page.route("**/api/v1/ledger/recent**", recent)
    page.route("**/api/v1/adjudicate", adjudicate)
    page.route_web_socket("**/ws/vps", socket)
    page.goto(cockpit_origin + "/", wait_until="domcontentloaded")
    _connect(page)
    page.wait_for_function(
        "() => document.getElementById('connHint').textContent.startsWith('Connected')"
    )
    page.wait_for_function("() => document.getElementById('estream').innerText.includes('<script>')")
    _assert_inert(page, "#estream")
    assert "<img" in page.locator("#estream").inner_text()

    page.click(".tab[data-tab='forensics']")
    page.click("#btnLoad")
    page.wait_for_function("() => document.getElementById('timeline').innerText.includes('<script>')")
    _assert_inert(page, "#timeline")
    assert "<img" in page.locator("#timeline").inner_text()
    _shot(page, "pr148_xss_rendered_as_text.png")

    page.click(".tab[data-tab='cockpit']")
    page.click("#adjBtn")
    page.wait_for_function("() => document.getElementById('adjDetail').innerText.includes('<script>')")
    _assert_inert(page, "#adjDetail")
    page.close()
