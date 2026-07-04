"""Part 19 — Control Surface Gateway. Authority is invariant under substitution of the SURFACE:
phone, tablet, Alexa, Google, HA Assist all collapse to (Intent, Operator) and face the one
router. The gateway adds device auth + a pending registry, but NO permission logic — so a
per-surface policy can never diverge from the engine. Everything below drives the socketless
core; the HTTP layer is a thin JSON wrapper over exactly these calls."""
from email.message import Message

import pytest

from homeops import build_world
from homeops.gateway import Gateway
from homeops.gateway.api import make_handler


@pytest.fixture
def gw():
    w = build_world(register_automations=False)
    g = Gateway(w)
    # a small estate of surfaces, each a Principal with its role cap + scope:
    g._t = {
        "phone":  g.enroll("cole_iphone",    "owner",          houses="*", surface="phone"),
        "tablet": g.enroll("kitchen_tablet", "estate_manager", houses=["house_a"], surface="tablet"),
        "alexa":  g.enroll("kitchen_echo",   "monitor",        houses=["house_a"], surface="alexa"),
        "guest":  g.enroll("guest_phone",    "guest",          houses=["house_a"], surface="phone"),
    }
    return g


def _intent(house="house_a", subsystem="light", target="kitchen", action="turn_on", **args):
    return {"house_id": house, "subsystem": subsystem, "target": target, "action": action,
            "args": args}


# ---- L1 executes directly from any legitimate surface ----------------------------------
def test_l1_executes_from_phone_and_tablet_and_voice(gw):
    for k in ("phone", "tablet", "alexa"):
        r = gw.submit_intent(gw._t[k], _intent(action="turn_on"))
        assert r["status"] == "executed", (k, r)
    assert gw.world.state.get_state("house_a.light.kitchen") == "on"


def test_unknown_device_token_is_unauthorized(gw):
    assert gw.submit_intent("not-a-real-token", _intent())["status"] == "unauthorized"


def test_malformed_intent_is_bad_request_not_pending(gw):
    r = gw.submit_intent(gw._t["phone"], {"house_id": "house_a", "subsystem": "light"})
    assert r["status"] == "bad_request"
    assert gw.list_pending(gw._t["phone"])["pending"] == []


# ---- L2 -> confirm_required, carrying the engine's attestation --------------------------
def test_l2_from_phone_returns_pending_with_effect(gw):
    r = gw.submit_intent(gw._t["phone"], _intent(subsystem="lock", target="front_door", action="unlock"))
    assert r["status"] == "confirm_required" and r["pending_id"]
    assert r["effect"] == "[L2] UNLOCK house_a/front_door"   # engine ground truth, not a paraphrase
    assert gw.world.state.get_state("house_a.lock.front_door") == "locked"   # nothing yet


def test_phone_confirms_and_executes_as_human(gw):
    r = gw.submit_intent(gw._t["phone"], _intent(subsystem="lock", target="front_door", action="unlock"))
    c = gw.confirm(gw._t["phone"], r["pending_id"])
    assert c["status"] == "executed" and c["confirmed_by"] == "cole_iphone"
    assert gw.world.state.get_state("house_a.lock.front_door") == "unlocked"
    # the confirm token never leaves the engine: it is not in any gateway response:
    assert "confirm_token" not in c and "confirm_token" not in r


def test_pending_is_consumed_after_confirm(gw):
    r = gw.submit_intent(gw._t["phone"], _intent(subsystem="garage", target="main", action="open"))
    gw.confirm(gw._t["phone"], r["pending_id"])
    assert gw.confirm(gw._t["phone"], r["pending_id"])["status"] == "not_found"


# ---- surface-invariance of authority: the cap is the OPERATOR, not the surface ----------
def test_voice_surface_cannot_execute_l2_it_is_capped_by_its_role(gw):
    # 'monitor' role => max_level 1. An L2 unlock from Alexa is refused by the ROUTER, not the gateway.
    r = gw.submit_intent(gw._t["alexa"], _intent(subsystem="lock", target="front_door", action="unlock"))
    assert r["status"] == "refused" and r["level"] == 2
    assert gw.world.state.get_state("house_a.lock.front_door") == "locked"


def test_guest_capped_at_l1(gw):
    assert gw.submit_intent(gw._t["guest"], _intent(action="turn_on"))["status"] == "executed"
    assert gw.submit_intent(gw._t["guest"],
                            _intent(subsystem="lock", target="front_door", action="unlock"))["status"] == "refused"


def test_tablet_out_of_scope_house_is_refused(gw):
    # kitchen_tablet is scoped to house_a only; house_b is out of scope (router RBAC).
    r = gw.submit_intent(gw._t["tablet"], _intent(house="house_b", action="turn_on"))
    assert r["status"] == "refused" and "scope" in r["message"].lower()


# ---- cross-surface confirmation: a low-trust surface proposes, a confirming surface disposes
def test_alexa_proposes_l2_via_owner_but_only_confirming_surface_approves(gw):
    # Give Alexa enough level to *create* the pending by using the owner phone to propose,
    # then show a non-confirming device cannot approve while the phone can. First, the
    # 'monitor' alexa cannot even create an L2 pending (capped), so the phone proposes:
    r = gw.submit_intent(gw._t["phone"], _intent(subsystem="garage", target="main", action="open"))
    pid = r["pending_id"]
    # a guest (can_confirm=False) is refused at the gateway confirm gate:
    assert gw.confirm(gw._t["guest"], pid)["status"] == "refused"
    # the pending survives a rejected confirm attempt and the phone still approves it:
    assert gw.confirm(gw._t["phone"], pid)["status"] == "executed"


def test_deny_removes_pending_and_is_audited(gw):
    r = gw.submit_intent(gw._t["phone"], _intent(subsystem="lock", target="front_door", action="unlock"))
    d = gw.deny(gw._t["phone"], r["pending_id"], reason="not now")
    assert d["status"] == "denied"
    assert gw.confirm(gw._t["phone"], r["pending_id"])["status"] == "not_found"
    assert any(rec.status == "recommended" and "DENIED" in rec.message for rec in gw.world.audit.records)


# ---- the attestation guard survives the network boundary --------------------------------
def test_tampered_pending_attestation_is_refused_at_confirm(gw):
    r = gw.submit_intent(gw._t["phone"], _intent(subsystem="lock", target="front_door", action="unlock"))
    pid = r["pending_id"]
    # a compromised store / MITM rewrites the held effect to look benign:
    gw._pending[pid].attestation.statement["effect"] = "[L1] turn_on house_a/living_room"
    c = gw.confirm(gw._t["phone"], pid)
    assert c["status"] == "refused" and "attestation" in c["message"]
    assert gw.world.state.get_state("house_a.lock.front_door") == "locked"


# ---- pending TTL --------------------------------------------------------------------------
def test_pending_expires(gw):
    g = gw
    g2 = Gateway(g.world, identity=g.identity, pending_ttl=5)
    tok = g2.enroll("temp_phone", "owner", surface="phone")
    r = g2.submit_intent(tok, _intent(subsystem="lock", target="front_door", action="unlock"))
    g.world.router.engine.tick += 6   # advance past the TTL
    assert g2.confirm(tok, r["pending_id"])["status"] == "not_found"


# ---- reads are scoped -----------------------------------------------------------------
def test_state_is_scoped_to_the_device(gw):
    both = gw.state(gw._t["phone"])["houses"]
    assert set(both) == {"house_a", "house_b"}
    just_a = gw.state(gw._t["tablet"])["houses"]
    assert set(just_a) == {"house_a"}     # tablet scoped to house_a only


def test_http_gateway_token_required_before_device_auth(gw):
    Handler = make_handler(gw, gateway_token="transport-secret")

    def request(headers):
        h = Handler.__new__(Handler)
        h.path = "/v1/state"
        h.headers = Message()
        for k, v in headers.items():
            h.headers[k] = v
        captured = {}
        h._send = lambda code, obj: captured.update(code=code, obj=obj)
        h.do_GET()
        return captured["code"], captured["obj"]

    bearer = {"Authorization": f"Bearer {gw._t['phone']}"}
    assert request(bearer)[0] == 401
    wrong = {**bearer, "X-Homeops-Gateway-Token": "wrong"}
    assert request(wrong)[0] == 401
    ok = {**bearer, "X-Homeops-Gateway-Token": "transport-secret"}
    status, body = request(ok)
    assert status == 200 and set(body["houses"]) == {"house_a", "house_b"}


def test_out_of_scope_device_cannot_deny_or_confirm_other_house_pending(gw):
    r = gw.submit_intent(gw._t["phone"],
                         _intent(house="house_b", subsystem="lock", target="front_door", action="unlock"))
    pid = r["pending_id"]

    assert gw.deny(gw._t["tablet"], pid)["status"] == "not_found"
    assert gw.confirm(gw._t["phone"], pid)["status"] == "executed"
    assert gw.world.state.get_state("house_b.lock.front_door") == "unlocked"

    r2 = gw.submit_intent(gw._t["phone"],
                          _intent(house="house_b", subsystem="garage", target="main", action="open"))
    pid2 = r2["pending_id"]
    assert gw.confirm(gw._t["tablet"], pid2)["status"] == "not_found"
    assert gw.confirm(gw._t["phone"], pid2)["status"] == "executed"
