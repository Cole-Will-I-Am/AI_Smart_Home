"""Part 7: the runtime service — live read-only surface, token gate, clean shutdown."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from homeops.deployment import DeploymentConfig
from homeops.service import Service


def _svc(tmp_path, token=""):
    dep = DeploymentConfig(dash_port=0, tick_seconds=0.05,
                           audit_path=str(tmp_path / "audit.jsonl"),
                           state_dir=str(tmp_path))
    secrets = {"HOMEOPS_DASH_TOKEN": token} if token else {}
    return Service(dep, secrets=secrets)


def _get(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode()


def _run_bg(svc, cycles=None):
    t = threading.Thread(target=svc.run, kwargs={"install_signals": False, "max_cycles": cycles})
    t.start()
    return t


def test_healthz_and_dashboard_served(tmp_path):
    svc = _svc(tmp_path)
    t = _run_bg(svc)
    try:
        time.sleep(0.2)
        port = svc._httpd.server_address[1]
        status, body = _get(port, "/healthz")
        h = json.loads(body)
        assert status == 200 and h["mode"] == "sim" and h["audit"]["chain_ok"] is True
        status, html = _get(port, "/")
        assert status == 200 and "house_a" in html
    finally:
        svc.stop()
        t.join(timeout=5)
    assert not t.is_alive()


def test_token_gate_when_configured(tmp_path):
    svc = _svc(tmp_path, token="s3cret")
    t = _run_bg(svc)
    try:
        time.sleep(0.2)
        port = svc._httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(port, "/healthz")
        assert e.value.code == 401
        status, _ = _get(port, "/healthz", token="s3cret")
        assert status == 200
    finally:
        svc.stop()
        t.join(timeout=5)


def test_write_verbs_refused(tmp_path):
    svc = _svc(tmp_path)
    t = _run_bg(svc)
    try:
        time.sleep(0.2)
        port = svc._httpd.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 405            # the surface has no write path, by design
    finally:
        svc.stop()
        t.join(timeout=5)


def test_nonloopback_bind_without_token_refused(tmp_path):
    dep = DeploymentConfig(dash_host="0.0.0.0", dash_port=0)
    with pytest.raises(RuntimeError, match="validation failed"):
        Service(dep, secrets={})


def test_bounded_run_flushes_status_and_persists_audit(tmp_path):
    svc = _svc(tmp_path)
    rc = svc.run(install_signals=False, max_cycles=3)
    assert rc == 0 and svc.cycles >= 3
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["audit"]["chain_ok"] is True
    # audit chain file exists and reloads verified on a fresh world
    from homeops.audit import AuditLog
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    ok, _ = log.verify_chain()
    assert ok


def test_sim_world_actually_ticks(tmp_path):
    svc = _svc(tmp_path)
    before = svc.world.engine.tick
    svc.run(install_signals=False, max_cycles=4)
    assert svc.world.engine.tick > before
