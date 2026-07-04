#!/usr/bin/env python3
"""End-to-end two-house demo (no network / no hardware).

Drives both houses through the headline behaviours: house-scoped commands, the cross-house
confirmation guard, a two-signal leak auto-shutoff, a WAN-down local-first proof, and the AI
layer proposing an L4 action that the engine refuses.
"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homeops import build_world
from homeops.permissions import Intent, Operator
from homeops.simulator import scenarios
from homeops.ai import OpsLayer


def line(msg): print("• " + msg)


def main():
    w = build_world()
    owner_a = Operator("owner", "house_a", "cole")

    print("\n# 1. House-scoped routine command (L1) — House A")
    r = w.router.execute(Intent("house_a", "alarm", "panel", "arm", {"mode": "night"}), owner_a)
    line(f"arm house_a night -> {r.status}: {r.message}")

    print("\n# 2. In-range thermostat on House B (direct) via explicit cross-house confirm")
    r = w.router.execute(Intent("house_b", "climate", "thermostat_main", "set_temperature",
                                {"temperature": 68}, confirm_cross_house=True), owner_a)
    line(f"set house_b thermostat 68 (confirmed cross-house) -> {r.status}: {r.message}")

    print("\n# 3. Cross-house guard: same command WITHOUT confirmation is blocked")
    r = w.router.execute(Intent("house_b", "light", "kitchen", "turn_on"), owner_a)
    line(f"turn on house_b kitchen light (no confirm) -> {r.status}: {r.message}")

    print("\n# 4. Two-signal leak: main water auto-shuts off (local, no AI)")
    scenarios.leak(w, "house_a")
    w.tick(2)
    line(f"house_a main_valve -> {w.state.get_state('house_a.water.main_valve')}")

    print("\n# 5. Local-first: drop WAN, trigger a fire — locals still respond")
    scenarios.wan_failure(w, "house_b")
    scenarios.fire_co(w, "house_b")
    line(f"house_b egress lock -> {w.state.get_state('house_b.lock.egress_side')} "
         f"(WAN down, handled locally)")

    print("\n# 6. AI ops layer proposes an L4 action — engine refuses (no execution path)")
    OpsLayer(w, client=None)        # client None -> fallback; L4 shown directly via router:
    r = w.router.execute(Intent("house_a", "lock", "front_door", "unlock_unknown"),
                         Operator("ai", "house_a", "ai-ops"))
    line(f"ai proposes unlock_unknown -> {r.status}: {r.message}")

    refusals = [x for x in w.audit.records if x.status in ("recommend_only", "prohibited", "refused")]
    print(f"\nAudit: {len(w.audit.records)} records, {len(refusals)} refusals/recommend-only (all logged).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
