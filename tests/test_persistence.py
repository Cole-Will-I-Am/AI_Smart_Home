"""R6 regression witnesses — durable authority state across a restart.

Enrollments and standing consent must survive process death; a restart is modelled by building
a second store/world against the same persistence path and asserting the authority carries over.
Pendings are deliberately NOT persisted (tick-scoped, attestation-bound consent-in-flight), so
there is no test for their survival — that is the intended behaviour.
"""
from homeops import build_world
from homeops.gateway import Gateway
from homeops.permissions import Intent, Operator
from homeops.identity import IdentityStore
from datetime import datetime

from homeops.delegations import DelegationRegistry, Delegation


# --- IdentityStore -------------------------------------------------------------------------
def test_enrollment_and_revocation_persist(tmp_path):
    path = str(tmp_path / "identity.json")
    s1 = IdentityStore(path=path)
    tok = s1.register("phone1", "owner", houses="*")
    scoped = s1.register("tablet1", "estate_manager", houses=["house_a"])

    s2 = IdentityStore(path=path)                    # "restart"
    p = s2.authenticate(tok)
    assert p is not None and p.id == "phone1" and p.houses == "*"
    assert s2.authenticate(scoped).houses == frozenset({"house_a"})   # scope round-trips

    assert s2.revoke(tok) is True                    # revoke, then restart again
    s3 = IdentityStore(path=path)
    assert s3.authenticate(tok) is None and s3.authenticate(scoped) is not None


def test_expiry_persists(tmp_path):
    path = str(tmp_path / "identity.json")
    s1 = IdentityStore(path=path)
    live = s1.register("dev_live", "owner", ttl_seconds=3600)
    dead = s1.register("dev_dead", "owner", ttl_seconds=-1)   # already expired
    s2 = IdentityStore(path=path)
    assert s2.authenticate(live) is not None
    assert s2.authenticate(dead) is None             # expiry survived the reload


# --- DelegationRegistry --------------------------------------------------------------------
def test_standing_consent_and_budget_persist(tmp_path):
    path = str(tmp_path / "delegations.json")
    owner = Operator("owner", "house_a", "cole")
    r1 = DelegationRegistry(path=path)
    d = r1.grant(Delegation("d1", "cole", "house_a", "light", "turn_on", budget_per_day=4), owner)
    # model a real same-day consumption: a match stamps the day, then budget is spent.
    d._day = datetime.now().date()
    d.used_today = 2
    r1.persist()

    r2 = DelegationRegistry(path=path)               # "restart"
    m = r2.match(Intent("house_a", "light", "kitchen", "turn_on"))
    assert m is not None and m.id == "d1"
    assert m.used_today == 2, "a consumed daily budget must not reset on restart"

    assert r2.revoke("d1") is True
    r3 = DelegationRegistry(path=path)
    assert r3.match(Intent("house_a", "light", "kitchen", "turn_on")) is None


# --- end to end through build_world ---------------------------------------------------------
def test_enrollment_survives_a_world_restart(tmp_path):
    d = str(tmp_path)
    w1 = build_world(register_automations=False, persist_dir=d)
    tok = Gateway(w1).enroll("cole_phone", "owner", houses="*", surface="phone")

    w2 = build_world(register_automations=False, persist_dir=d)   # a fresh process/world
    assert w2.identity.authenticate(tok) is not None, "the device is still enrolled after restart"
    r = Gateway(w2).submit_intent(tok, {"house_id": "house_a", "subsystem": "light",
                                        "target": "kitchen", "action": "turn_on", "args": {}})
    assert r["status"] == "executed", "the persisted credential still actuates after restart"


def test_delegation_survives_a_world_restart(tmp_path):
    d = str(tmp_path)
    w1 = build_world(register_automations=False, persist_dir=d)
    w1.delegations.grant(Delegation("d_night", "cole", "house_a", "light", "turn_on"),
                         Operator("owner", "house_a", "cole"))
    w2 = build_world(register_automations=False, persist_dir=d)
    assert w2.delegations.match(Intent("house_a", "light", "kitchen", "turn_on")) is not None
