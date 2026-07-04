"""Regression witnesses for the production-hardening pass (audit findings R2, R3, R5, R8, R9).

These target the *runtime the product ships* — threaded writers, adapter faults, credential
revocation, emergency throttling, cross-scope reads — which the pre-existing suite (green in sim,
single-threaded) never exercised. Each test fails against the code as audited and passes after the
corresponding fix.
"""
import threading
import urllib.request
import urllib.error

import pytest

from homeops import build_world
from homeops.gateway import Gateway
from homeops.permissions import Intent, Operator
from homeops.audit import AuditLog, AuditRecord
from homeops.identity import IdentityStore
from homeops.events import Event
from homeops.adapters.http import urllib_transport


# --- R3: an adapter fault is recorded truthfully, never raised -----------------------------
def test_adapter_fault_is_audited_as_error_not_raised(bare):
    class BoomAdapter:
        def apply(self, intent):
            raise TimeoutError("HA unreachable")   # a socket timeout escaping the transport
        def undo(self, undo):
            pass
    bare.router.adapter = BoomAdapter()
    before = len(bare.router.audit.records)
    r = bare.router.execute(Intent("house_a", "light", "kitchen", "turn_on"),
                            Operator("owner", "house_a", "cole"))
    assert r.status == "error" and "UNKNOWN" in r.message
    recs = bare.router.audit.records
    assert len(recs) == before + 1 and recs[-1].status == "error", "the fault must leave an audit record"


def test_transport_timeout_becomes_504_not_exception(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    status, text = urllib_transport(timeout=0.01)("POST", "https://ha.local/api/x", {}, "{}")
    assert status == 504 and "transport" in text, "a transport failure must surface as a clean status"


# --- R2: the hash chain survives concurrent writers ----------------------------------------
def test_audit_chain_survives_concurrent_writers():
    log = AuditLog()

    def worker(i):
        for j in range(50):
            log.record(AuditRecord(0, "ai", "house_a", "light", "k", "turn_on", {}, 1,
                                   "executed", f"{i}:{j}"))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ok, bad = log.verify_chain()
    assert ok and len(log.records) == 400, (ok, bad, len(log.records))


# --- R5: credentials can be revoked and can expire -----------------------------------------
def test_identity_revocation_and_expiry():
    s = IdentityStore()
    tok = s.register("dev1", "owner")
    assert s.authenticate(tok) is not None
    assert s.revoke(tok) is True
    assert s.authenticate(tok) is None                 # revoked -> no longer authenticates
    assert s.revoke(tok) is False                      # idempotent

    a = s.register("bob", "guest", houses=["house_a"])
    b = s.register("bob", "guest", houses=["house_a"])
    assert s.revoke_principal("bob") == 2              # cut every credential a departed delegate holds
    assert s.authenticate(a) is None and s.authenticate(b) is None

    expired = s.register("dev2", "owner", ttl_seconds=-1)
    assert s.authenticate(expired) is None             # already past expiry -> auto-purged on use


# --- R8: an emergency bypasses rate limiting, but cooldown still protects hardware ----------
def test_emergency_bypasses_rate_but_not_cooldown(bare):
    eng = bare.engine
    normal = Intent("house_a", "light", "k", "turn_on")
    for _ in range(eng._rate_limit):
        assert eng.allow_rate(normal) is True
    assert eng.allow_rate(normal) is False                                   # subsystem now capped
    assert eng.allow_rate(Intent("house_a", "light", "k", "turn_on", emergency=True)) is True  # R8

    g = Intent("house_a", "generator", "main", "start", emergency=True)
    assert eng.allow_cooldown(g) is True
    assert eng.allow_cooldown(g) is False              # cooldown STILL applies to emergencies (narrow scope)


# --- R9: a device sees only pending confirmations and events within its house scope ---------
def _two_scope_gateway():
    w = build_world(register_automations=False)
    g = Gateway(w)
    owner = g.enroll("owner_phone", "owner", houses="*", surface="phone")
    scoped = g.enroll("a_tablet", "estate_manager", houses=["house_a"], surface="tablet")
    return w, g, owner, scoped


def test_pending_is_scoped_to_the_devices_houses():
    w, g, owner, scoped = _two_scope_gateway()
    r = g.submit_intent(owner, {"house_id": "house_b", "subsystem": "lock",
                                "target": "front_door", "action": "unlock", "args": {}})
    assert r["status"] == "confirm_required" and r["pending_id"]
    assert any(p["pending_id"] == r["pending_id"] for p in g.list_pending(owner)["pending"])
    scoped_pending = g.list_pending(scoped)["pending"]
    assert all(p["intent"]["house_id"] == "house_a" for p in scoped_pending)
    assert not any(p["pending_id"] == r["pending_id"] for p in scoped_pending)


def test_events_are_scoped_to_the_devices_houses():
    w, g, owner, scoped = _two_scope_gateway()
    w.bus.publish(Event("leak", "house_a", "house_a.sensor.leak", {}, 0))
    w.bus.publish(Event("leak", "house_b", "house_b.sensor.leak", {}, 0))
    evs = g.events(scoped)["events"]
    assert evs and all(e["house"] == "house_a" for e in evs)
    assert g.events(scoped, house_id="house_b")["events"] == []   # explicit out-of-scope request
