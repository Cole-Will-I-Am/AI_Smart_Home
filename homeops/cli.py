"""Tiny operator CLI: show both houses, issue a house-scoped command, watch the audit log.

    python -m homeops.cli status
    python -m homeops.cli command house_a light living_room turn_on
"""
from __future__ import annotations
import sys
from .bootstrap import build_world
from .permissions import Intent, Operator


def status(world) -> None:
    for hid, house in world.houses.items():
        print(f"== {hid} ({house.alias}) mode={house.mode} wan={'up' if house.wan_up else 'DOWN'} "
              f"grid={'up' if house.grid_up else 'DOWN'} ai_hold={house.ai_hold}")
        for sub in ("lock", "alarm", "water", "power", "battery", "generator", "light"):
            ents = [e for e in house.entities.values() if e.subsystem == sub]
            if ents:
                print("   " + sub + ": " + ", ".join(f"{e.name}={e.state}" for e in ents))


def main(argv: list[str]) -> int:
    world = build_world()
    if not argv or argv[0] == "status":
        status(world)
        return 0
    if argv[0] == "command" and len(argv) >= 5:
        house, subsystem, target, action = argv[1:5]
        r = world.router.execute(Intent(house, subsystem, target, action),
                                 Operator("owner", house, "cli"))
        print(f"{r.status}: {r.message}" + (f"  (confirm token: {r.confirm_token})" if r.confirm_token else ""))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
