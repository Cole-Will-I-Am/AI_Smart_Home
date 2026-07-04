"""Long-running operations service — the missing operational layer.

One process per estate: builds the World from a deployment descriptor (sim or real), runs the
housekeeping loop (world tick in sim mode; audit-chain verification, dashboard re-render, and a
liveness heartbeat in both modes), optionally starts the HA WebSocket event bridge, and serves a
READ-ONLY HTTP surface:

    GET /            operator dashboard (HTML snapshot, re-rendered each cycle)
    GET /healthz     liveness JSON: uptime, mode, audit-chain integrity, per-house flags

Exposure is fail-closed: binding to a non-loopback address without a bearer token refuses to
start; when HOMEOPS_DASH_TOKEN is set, every request must carry `Authorization: Bearer <token>`.
There is deliberately NO write path here — commands enter only through the CLI/engine, so the
network surface cannot actuate anything even if compromised. SIGTERM/SIGINT stop the loop,
flush a final dashboard snapshot, and exit 0 (systemd-clean).
"""
from __future__ import annotations
import hmac
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bootstrap import World, build_world
from .dashboard import render_dashboard
from .deployment import DeploymentConfig, validate_deployment, has_failures, render_results
from .secrets import load_secrets, require, required_for_mode


def build_service_world(dep: DeploymentConfig, secrets: dict[str, str]) -> World:
    if dep.mode == "sim":
        return build_world(dep.houses_config, audit_path=dep.audit_path, persist_dir=dep.state_dir)
    from .bootstrap import build_real_world
    require(secrets, required_for_mode("real", opnsense=dep.opnsense))
    return build_real_world(
        ha_base_url=secrets["HOMEOPS_HA_URL"], ha_token=secrets["HOMEOPS_HA_TOKEN"],
        opn_base_url=secrets.get("HOMEOPS_OPN_URL", ""), opn_key=secrets.get("HOMEOPS_OPN_KEY", ""),
        opn_secret=secrets.get("HOMEOPS_OPN_SECRET", ""),
        config_path=dep.houses_config, entity_map=dep.entity_map, event_map=dep.event_map,
        verify_tls=dep.verify_tls)


class Service:
    def __init__(self, dep: DeploymentConfig, secrets: dict[str, str] | None = None,
                 world: World | None = None) -> None:
        self.dep = dep
        self.secrets = secrets if secrets is not None else load_secrets(dep.secrets_file)
        results = validate_deployment(dep, dash_token_present=bool(self.secrets.get("HOMEOPS_DASH_TOKEN")))
        if has_failures(results):
            raise RuntimeError("deployment validation failed:\n" + render_results(results, "validate"))
        self.world = world or build_service_world(dep, self.secrets)
        self.started = time.time()
        self.cycles = 0
        self.audit_ok: bool = True
        self.audit_len: int = 0
        self._stop = threading.Event()
        self._dash_html = "<html><body>starting…</body></html>"
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._bridge_thread = None

    # ---- state consumed by the HTTP surface (read-only) ----------------------
    def status(self) -> dict:
        houses = {hid: {"mode": h.mode, "wan_up": h.wan_up, "grid_up": h.grid_up, "ai_hold": h.ai_hold}
                  for hid, h in self.world.houses.items()}
        return {"service": "homeops", "mode": self.dep.mode,
                "uptime_s": round(time.time() - self.started, 1), "cycles": self.cycles,
                "audit": {"chain_ok": self.audit_ok, "records": self.audit_len},
                "event_bridge": bool(self._bridge_thread and self._bridge_thread.is_alive()),
                "houses": houses}

    def _housekeep(self) -> None:
        if self.dep.mode == "sim":
            self.world.tick()               # advances engine.tick AND steps the sim world
        else:
            # H1: real mode has no sim world to step, but the engine's monotonic clock must
            # still advance — health staleness, confirmation-token TTLs, and destructive-action
            # cooldowns are all measured in ticks. A frozen clock makes "stale" unreachable and
            # traps one-shot cooldowns forever. One tick per housekeeping cycle is the real-mode
            # operational clock (period = dep.tick_seconds).
            self.world.engine.tick += 1
        ok = self.world.audit.verify_incremental()   # M4: O(new records), not O(whole chain)
        self.audit_ok, self.audit_len = ok, len(self.world.audit.records)
        html = render_dashboard(self.world)
        with self._lock:
            self._dash_html = html
        self.cycles += 1
        if self.dep.state_dir:
            os.makedirs(self.dep.state_dir, exist_ok=True)
            tmp = os.path.join(self.dep.state_dir, ".status.tmp")
            with open(tmp, "w") as f:
                json.dump(self.status(), f)
            os.replace(tmp, os.path.join(self.dep.state_dir, "status.json"))

    # ---- read-only HTTP surface ----------------------------------------------
    def _make_handler(self):
        svc = self
        token = self.secrets.get("HOMEOPS_DASH_TOKEN", "")

        class Handler(BaseHTTPRequestHandler):
            server_version = "homeops"

            def _authorized(self) -> bool:
                if not token:
                    return True     # only reachable in loopback binds (validated at startup)
                got = self.headers.get("Authorization", "")
                return hmac.compare_digest(got, f"Bearer {token}")

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 — http.server API
                if not self._authorized():
                    return self._send(401, b'{"error":"bearer token required"}', "application/json")
                if self.path == "/healthz":
                    return self._send(200, json.dumps(svc.status()).encode(), "application/json")
                if self.path == "/":
                    with svc._lock:
                        body = svc._dash_html.encode()
                    return self._send(200, body, "text/html; charset=utf-8")
                return self._send(404, b'{"error":"read-only surface: / and /healthz"}', "application/json")

            def do_POST(self):  # noqa: N802 — explicitly refuse any write verb
                self._send(405, b'{"error":"this surface is read-only by design"}', "application/json")
            do_PUT = do_DELETE = do_PATCH = do_POST

            def log_message(self, *a):  # quiet; audit is the log of record
                pass

        return Handler

    # ---- lifecycle -------------------------------------------------------------
    def start_http(self) -> int:
        self._httpd = ThreadingHTTPServer((self.dep.dash_host, self.dep.dash_port), self._make_handler())
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self._httpd.server_address[1]

    def stop(self, *_sig) -> None:
        self._stop.set()

    def run(self, install_signals: bool = True, max_cycles: int | None = None) -> int:
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        port = self.start_http()
        if self.dep.mode == "real" and self.dep.event_bridge:
            from .bootstrap import start_event_bridge
            self._bridge_thread = start_event_bridge(self.world)
        self._housekeep()   # first render before first sleep
        print(f"homeops service up: mode={self.dep.mode} dash=http://{self.dep.dash_host}:{port} "
              f"(read-only{', token-gated' if self.secrets.get('HOMEOPS_DASH_TOKEN') else ''})")
        while not self._stop.is_set():
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            self._stop.wait(self.dep.tick_seconds)
            if not self._stop.is_set():
                self._housekeep()
        self._housekeep()   # final flush
        if self._httpd:
            self._httpd.shutdown()
        print(f"homeops service stopped cleanly after {self.cycles} cycles "
              f"(audit chain ok={self.audit_ok}, {self.audit_len} records)")
        return 0
