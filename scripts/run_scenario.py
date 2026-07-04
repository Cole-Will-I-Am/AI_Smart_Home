#!/usr/bin/env python3
"""Replay emergency scenarios, assert the outcome, and print the event->action timeline.

    python scripts/run_scenario.py all
    python scripts/run_scenario.py leak
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homeops import build_world
from homeops.simulator import scenarios


def _timeline(world, title):
    print(f"\n=== {title} ===")
    for r in world.audit.records:
        if r.status in ("executed", "recommended"):
            print(f"  [{r.operator}] {r.house_id}.{r.subsystem}.{r.target}.{r.action} -> {r.status}: {r.message}")
    for n in world.notifications:
        flag = "URGENT" if n["urgent"] else "info"
        print(f"  ({flag}) {n['house_id']}: {n['message']}")


def leak():
    w = build_world()
    scenarios.leak(w, "house_a")
    w.tick(2)
    assert w.state.get_state("house_a.water.main_valve") == "closed"
    _timeline(w, "LEAK (two-signal): main valve auto-closes")


def grid():
    w = build_world()
    scenarios.grid_failure(w, "house_a")
    assert w.state.get_state("house_a.battery.main") == "backup"
    _timeline(w, "GRID FAILURE: battery backup + load shed")


def fire():
    w = build_world()
    scenarios.fire_co(w, "house_a")
    assert w.state.get_state("house_a.lock.egress_side") == "unlocked"
    assert w.state.get_state("house_a.hvac.main") == "off"
    _timeline(w, "FIRE/CO: egress unlock + HVAC stop")


def intrusion():
    w = build_world()
    scenarios.intrusion(w, "house_a")
    assert w.state.get_state("house_a.lock.front_door") == "locked"
    _timeline(w, "INTRUSION: exterior lock + lights + record")


def rogue():
    w = build_world()
    scenarios.rogue_device(w, "house_a")
    assert w.net.vlan_of("house_a", "3c:6a:9d:aa:bb:cc") == "iot_guest"
    _timeline(w, "ROGUE DEVICE: quarantined to isolated VLAN")


ALL = {"leak": leak, "grid": grid, "fire": fire, "intrusion": intrusion, "rogue": rogue}


def main(argv):
    which = argv[0] if argv else "all"
    todo = ALL.values() if which == "all" else [ALL[which]]
    for fn in todo:
        fn()
    print("\nAll scenario assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
