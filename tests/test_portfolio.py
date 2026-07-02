"""Part 4 — multi-property control plane (N properties, portfolio view, per-property routing)."""
import os
from homeops import build_world
from homeops.portfolio import portfolio_summary
from homeops.simulator import scenarios
from homeops.adapters.per_property import PerPropertyAdapter
from homeops.adapters.base import Adapter
from homeops.permissions import Intent

CFG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "portfolio.example.yaml")


def test_loads_n_properties():
    w = build_world(CFG)
    assert len(w.houses) == 3 and "ski_chalet" in w.houses


def test_portfolio_summary_aggregates_and_isolates():
    w = build_world(CFG)
    ids = list(w.houses)
    scenarios.leak(w, ids[0])
    w.tick(2)
    w.health.mark_offline(f"{ids[1]}.lock.front_door")
    s = portfolio_summary(w)
    assert s["n_properties"] == 3
    assert s["total_offline_devices"] >= 1
    assert s["total_urgent_alerts"] >= 1          # the leak raised an urgent alert
    assert s["audit_intact"] is True              # estate-wide audit chain verifies
    # per-property isolation: the leak in property 0 did not touch property 2's valve
    assert w.state.get_state(f"{ids[2]}.water.main_valve") == "open"
    assert s["properties"][ids[0]]["safety"]["main_valve"] == "closed"


class _Rec(Adapter):
    def __init__(self, name):
        self.name, self.seen = name, []

    def apply(self, intent):
        self.seen.append(intent.house_id)
        return {"ok": True, "message": self.name, "undo": {"x": 1}}

    def undo(self, u):
        self.seen.append(("undo", u))


def test_per_property_adapter_routes_by_house_and_undo():
    a, b = _Rec("A"), _Rec("B")
    per = PerPropertyAdapter({"house_a": a, "house_b": b})
    res = per.apply(Intent("house_b", "light", "kitchen", "turn_on"))
    assert b.seen == ["house_b"] and a.seen == []
    per.undo(res["undo"])                          # routes back to B and unwraps the inner undo
    assert b.seen[-1] == ("undo", {"x": 1})


def test_per_property_adapter_refuses_unknown_property():
    per = PerPropertyAdapter({"house_a": _Rec("A")})
    res = per.apply(Intent("house_z", "light", "kitchen", "turn_on"))
    assert not res["ok"] and "no adapter" in res["message"]
