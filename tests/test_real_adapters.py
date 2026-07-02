"""Offline tests for the real HA / OPNsense adapters, driven by a fake HTTP transport and a
fake WebSocket connection — no network, no live services, no extra dependencies."""
import json

from homeops.permissions import Intent
from homeops.events import EventBus
from homeops.adapters import HomeAssistantAdapter, OPNsenseAdapter, CompositeAdapter
from homeops.adapters.base import Adapter


class FakeTransport:
    """Records requests; returns canned (status, text) by (method, url-substring).

    `get_states` (optional): a queue of states returned by successive GET /api/states/ calls, so a
    test can give a different prior-state and post-actuation-verification-state read."""
    def __init__(self, responses=None, get_states=None):
        self.calls = []
        self.responses = responses or {}
        self.get_states = list(get_states) if get_states is not None else None

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        if method == "GET" and "/api/states/" in url and self.get_states is not None:
            st = self.get_states.pop(0) if self.get_states else "unknown"
            return 200, json.dumps({"state": st})
        for (m, sub), (status, text) in self.responses.items():
            if m == method and sub in url:
                return status, text
        return 200, "[]"

    def posts(self, needle):
        return [c for c in self.calls if c["method"] == "POST" and needle in c["url"]]


def _state(state):
    return {("GET", "/api/states/"): (200, json.dumps({"state": state}))}


# --- Home Assistant REST -----------------------------------------------------

def test_ha_light_turn_on_and_undo():
    tr = FakeTransport(_state("off"))
    ha = HomeAssistantAdapter("http://ha:8123", "TOKEN", transport=tr)
    res = ha.apply(Intent("house_a", "light", "living_room", "turn_on"))
    assert res["ok"]
    call = tr.posts("/api/services/light/turn_on")[0]
    assert call["body"]["entity_id"] == "light.living_room"
    assert call["headers"]["Authorization"] == "Bearer TOKEN"
    # prior state was off -> undo is turn_off
    assert res["undo"]["ha_undo"]["service"] == "turn_off"
    ha.undo(res["undo"])
    assert tr.posts("/api/services/light/turn_off")


def test_ha_entity_map_and_lock_reversible():
    # prior read = "locked" (so undo re-locks); post-verification read = "unlocked" (command confirmed)
    tr = FakeTransport(get_states=["locked", "unlocked"])
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=tr,
                              entity_map={"house_a.lock.front_door": "lock.front_a"})
    res = ha.apply(Intent("house_a", "lock", "front_door", "unlock"))
    assert res["ok"]
    call = tr.posts("/api/services/lock/unlock")[0]
    assert call["body"]["entity_id"] == "lock.front_a"        # mapped, not the default lock.front_door
    assert res["undo"]["ha_undo"]["service"] == "lock"        # prior locked -> re-lock on undo


def test_ha_safety_action_verified_and_unverified():
    # valve reports "closed" after close_valve -> executed
    ok_tr = FakeTransport(get_states=["open", "closed"])
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=ok_tr)
    assert ha.apply(Intent("house_a", "water", "main_valve", "shutoff_main"))["ok"]
    # valve still "open" after close_valve -> NOT executed (HTTP 200 is not proof it actuated)
    bad_tr = FakeTransport(get_states=["open", "open"])
    ha2 = HomeAssistantAdapter("http://ha:8123", "T", transport=bad_tr)
    res = ha2.apply(Intent("house_a", "water", "main_valve", "shutoff_main"))
    assert not res["ok"] and "UNVERIFIED" in res["message"]


def test_ha_adapter_is_fail_closed_on_unknown_action():
    # unlock_unknown (L4, blocked by the router) must NOT map to lock.unlock at the adapter either
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=FakeTransport())
    res = ha.apply(Intent("house_a", "lock", "front_door", "unlock_unknown"))
    assert not res["ok"]


def test_ha_strict_entity_map_refuses_unmapped():
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=FakeTransport(get_states=["off"]),
                              strict_entity_map=True)   # no entity_map provided
    res = ha.apply(Intent("house_a", "light", "living_room", "turn_on"))
    assert not res["ok"] and "strict" in res["message"]


def test_ha_set_temperature_carries_data_and_is_not_reversible():
    tr = FakeTransport(_state("heat"))
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=tr)
    res = ha.apply(Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 68}))
    call = tr.posts("/api/services/climate/set_temperature")[0]
    assert call["body"]["temperature"] == 68
    assert res["undo"] is None                               # set-value has no clean inverse


def test_ha_unmapped_action_is_refused():
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=FakeTransport())
    res = ha.apply(Intent("house_a", "camera", "front_door", "set_mode", {"mode": "event"}))
    assert not res["ok"] and "no HA mapping" in res["message"]


def test_ha_http_error_surfaces():
    tr = FakeTransport({("POST", "/api/services/"): (500, "boom")})
    ha = HomeAssistantAdapter("http://ha:8123", "T", transport=tr)
    res = ha.apply(Intent("house_a", "light", "kitchen", "turn_on"))
    assert not res["ok"] and "HTTP 500" in res["message"]


# --- OPNsense REST -----------------------------------------------------------

def test_opnsense_quarantine_and_undo():
    tr = FakeTransport()
    opn = OPNsenseAdapter("https://opn.local", "KEY", "SECRET", transport=tr, verify_tls=False)
    res = opn.apply(Intent("house_a", "network", "firewall", "quarantine", {"ip": "10.10.20.50"}))
    assert res["ok"]
    add = tr.posts("/api/firewall/alias_util/add/quarantine")[0]
    assert add["body"]["address"] == "10.10.20.50"
    assert add["headers"]["Authorization"].startswith("Basic ")
    assert tr.posts("/api/firewall/alias/reconfigure")
    assert res["undo"]["opn_del"] == "10.10.20.50"
    opn.undo(res["undo"])
    assert tr.posts("/api/firewall/alias_util/delete/quarantine")


def test_opnsense_firewall_policy():
    tr = FakeTransport()
    opn = OPNsenseAdapter("https://opn.local", "K", "S", transport=tr)
    res = opn.apply(Intent("house_a", "network", "firewall", "firewall_policy",
                           {"rule": {"action": "block", "description": "isolate-iot"}}))
    assert res["ok"]
    assert tr.posts("/api/firewall/filter/addRule") and tr.posts("/api/firewall/filter/apply")


# --- Composite routing -------------------------------------------------------

class _Rec(Adapter):
    def __init__(self):
        self.seen = []

    def apply(self, intent):
        self.seen.append(intent.subsystem)
        return {"ok": True, "message": "rec", "undo": None}

    def undo(self, undo):
        pass


def test_composite_routes_network_to_opnsense_else_home():
    home, net = _Rec(), _Rec()
    comp = CompositeAdapter(home, net)
    comp.apply(Intent("house_a", "network", "firewall", "quarantine", {"ip": "x"}))
    comp.apply(Intent("house_a", "light", "kitchen", "turn_on"))
    assert net.seen == ["network"] and home.seen == ["light"]


# --- HA WebSocket event bridge ----------------------------------------------

class FakeWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def recv(self):
        return self.msgs.pop(0) if self.msgs else None


def test_ha_event_bridge_translates_state_changed():
    msgs = [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 1, "type": "result", "success": True}),
        json.dumps({"type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": "binary_sensor.leak_kitchen", "new_state": {"state": "on"}}}}),
    ]
    ws = FakeWS(msgs)
    bus = EventBus()
    ha = HomeAssistantAdapter("http://ha:8123", "TOK", transport=FakeTransport())
    event_map = {"binary_sensor.leak_kitchen": {"type": "leak", "when": "on",
                                                "house_id": "house_a", "data": {"flow": 45}}}
    ha.run_event_bridge(bus, event_map, connect=lambda: ws)
    leaks = [e for e in bus.history if e.type == "leak"]
    assert leaks and leaks[0].house_id == "house_a" and leaks[0].data["flow"] == 45
    assert any("access_token" in s for s in ws.sent)         # authenticated
    assert any("subscribe_events" in s for s in ws.sent)     # subscribed
