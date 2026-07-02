"""Tiny stdlib-only HTTP client used by the real adapters.

Deliberately built on urllib so the package needs no `requests`/`httpx` dependency. The
`transport` is injectable: tests pass a fake that records calls and returns canned JSON, so the
adapters are fully unit-testable offline with no network and no live HA/OPNsense.
"""
from __future__ import annotations
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable

# transport(method, url, headers, body) -> (status_code, response_text)
Transport = Callable[[str, str, dict, str | None], tuple[int, str]]


def urllib_transport(verify_tls: bool = True, timeout: float = 10.0) -> Transport:
    def _t(method: str, url: str, headers: dict, body: str | None) -> tuple[int, str]:
        data = body.encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        ctx = None
        if url.startswith("https") and not verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    return _t


class HttpClient:
    def __init__(self, base_url: str, default_headers: dict | None = None,
                 transport: Transport | None = None, verify_tls: bool = True, timeout: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.headers = default_headers or {}
        self.transport = transport or urllib_transport(verify_tls, timeout)

    def request(self, method: str, path: str, json_body: Any = None, headers: dict | None = None) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        hd = dict(self.headers)
        hd.update(headers or {})
        body = None
        if json_body is not None:
            hd.setdefault("Content-Type", "application/json")
            body = json.dumps(json_body)
        status, text = self.transport(method, url, hd, body)
        obj: Any = None
        if text:
            try:
                obj = json.loads(text)
            except ValueError:
                obj = text
        return status, obj
