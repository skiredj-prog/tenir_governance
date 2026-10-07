"""Standard-library HTTP adapter for a remote TENIR kernel API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .python import GovernanceBlockedError


class _RejectRedirects(HTTPRedirectHandler):
    """Keep action credentials and requests from being forwarded to redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener_for_url(url: str, *handlers):
    """Bypass configured proxies for loopback calls; preserve them remotely."""
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return build_opener(ProxyHandler({}), *handlers)
    return build_opener(*handlers)


class HTTPConnector:
    """Send canonical runtime objects to ``/api/v1/adjudicate``.

    Uses only the Python standard library. Keep the API token in an environment
    variable or secret manager; it is never included in exceptions or logs.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0) -> None:
        if not token.strip():
            raise ValueError("a non-empty TENIR API token is required")
        parts = urlsplit(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("base_url must be an http(s) URL without embedded credentials")
        if parts.scheme != "https" and parts.hostname.lower() not in local_hosts:
            raise ValueError("use HTTPS when sending a TENIR API token to a non-local host")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def adjudicate(
        self,
        runtime_object: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = json.dumps(
            {"runtime_object": dict(runtime_object)},
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/v1/adjudicate",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with _opener_for_url(self.base_url, _RejectRedirects).open(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"TENIR API returned HTTP {exc.code}") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"TENIR API request failed: {type(exc).__name__}") from None
        if not isinstance(result, dict):
            raise RuntimeError("TENIR API returned a non-object response")
        return result

    def execute(
        self,
        runtime_object: Mapping[str, Any],
        action_url: str,
        *,
        method: str = "POST",
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Adjudicate first, then make one HTTP action request if allowed.

        Action authorization headers are passed only to ``action_url`` and
        redirects are rejected so credentials cannot follow another origin.
        In shadow modes, intended blocks remain executable by design.
        """
        decision = self.adjudicate(runtime_object)
        if decision.get("execution_allowed") is not True:
            raise GovernanceBlockedError(decision)

        parts = urlsplit(action_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("action_url must be an http(s) URL without embedded credentials")
        if parts.scheme != "https" and parts.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("use HTTPS for non-local HTTP actions")
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("unsupported HTTP action method")

        if body is None:
            payload = None
        elif isinstance(body, bytes):
            payload = body
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        action_headers = dict(headers or {})
        if body is not None and not any(key.lower() == "content-type" for key in action_headers):
            action_headers["Content-Type"] = "application/json"
        request = Request(action_url, data=payload, method=method, headers=action_headers)
        try:
            with _opener_for_url(action_url, _RejectRedirects).open(request, timeout=self.timeout) as response:
                raw_body = response.read()
                response_headers = dict(response.headers.items())
                status = response.status
        except HTTPError as exc:
            raise RuntimeError(f"guarded HTTP action returned HTTP {exc.code}") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"guarded HTTP action failed: {type(exc).__name__}") from None

        text_body = raw_body.decode("utf-8", errors="replace")
        try:
            response_body = json.loads(text_body)
        except json.JSONDecodeError:
            response_body = text_body
        return {"status_code": status, "headers": response_headers, "body": response_body}, decision
