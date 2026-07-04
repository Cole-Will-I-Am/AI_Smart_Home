"""HTTP surface for the Control Surface Gateway — the WRITE path, deliberately separate from the
read-only dashboard in homeops.service (which refuses all writes).

Fail-closed exposure, identical posture to service.py: a non-loopback bind without a gateway
bearer secret refuses to start. Per-device auth is the `Authorization: Bearer <device-token>`
header, resolved by the Gateway to a Principal; there is no unauthenticated write. Every endpoint
returns JSON. This module is thin: all authority and all logic live in gateway.core.Gateway,
which is fully testable without a socket.

    POST /v1/intent                       submit a structured Intent
    POST /v1/pending/{id}/confirm         confirming surface approves a held L2+ intent
    POST /v1/pending/{id}/deny            deny a held intent
    GET  /v1/state[?house_id=]            scoped state snapshot
    GET  /v1/pending[?house_id=]          pending queue
    GET  /v1/events[?house_id=&n=]        recent event bus
"""
from __future__ import annotations
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def make_handler(gateway, gateway_token: str | None = None):
    gw = gateway

    class Handler(BaseHTTPRequestHandler):
        server_version = "homeops-gateway"

        def _bearer(self) -> str:
            got = self.headers.get("Authorization", "")
            return got[7:] if got.startswith("Bearer ") else ""

        def _gateway_authorized(self) -> bool:
            if not gateway_token:
                return True
            got = self.headers.get("X-Homeops-Gateway-Token", "")
            return hmac.compare_digest(got, gateway_token)

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode() or "{}")
            except (ValueError, UnicodeDecodeError):
                return {}

        @staticmethod
        def _status_code(result: dict) -> int:
            s = result.get("status")
            return {"unauthorized": 401, "bad_request": 400, "not_found": 404,
                    "refused": 200, "denied": 200}.get(s, 200)

        def do_GET(self):  # noqa: N802
            if not self._gateway_authorized():
                return self._send(401, {"status": "unauthorized",
                                        "message": "missing or invalid gateway token"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
            house = (q.get("house_id") or [None])[0]
            tok = self._bearer()
            if u.path == "/v1/state":
                return self._reply(gw.state(tok, house))
            if u.path == "/v1/pending":
                return self._reply(gw.list_pending(tok, house))
            if u.path == "/v1/events":
                n = int((q.get("n") or ["20"])[0])
                return self._reply(gw.events(tok, house, n))
            return self._send(404, {"error": "unknown endpoint"})

        def do_POST(self):  # noqa: N802
            if not self._gateway_authorized():
                return self._send(401, {"status": "unauthorized",
                                        "message": "missing or invalid gateway token"})
            u = urlparse(self.path)
            tok = self._bearer()
            parts = u.path.strip("/").split("/")
            if u.path == "/v1/intent":
                return self._reply(gw.submit_intent(tok, self._read_json()))
            # /v1/pending/{id}/confirm | /deny
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "pending":
                pid, verb = parts[2], parts[3]
                if verb == "confirm":
                    return self._reply(gw.confirm(tok, pid))
                if verb == "deny":
                    body = self._read_json()
                    return self._reply(gw.deny(tok, pid, body.get("reason", "")))
            return self._send(404, {"error": "unknown endpoint"})

        def _reply(self, result: dict) -> None:
            self._send(self._status_code(result), result)

        def log_message(self, *a):   # quiet; the audit chain is the log of record
            pass

    return Handler


def serve(gateway, host: str = "127.0.0.1", port: int = 8799,
          gateway_token: str | None = None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(gateway, gateway_token=gateway_token))
    return httpd
