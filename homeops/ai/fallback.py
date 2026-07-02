"""Deterministic local fallback used when the Claude API / internet is unavailable.

The house never depends on the AI: the local-first automations keep running on the event bus.
This fallback only covers *interactive operator goals* when the advisor is unreachable — it maps
a few obvious goals to engine-validated commands and otherwise reports that the advisory is
offline while locals stay active.
"""
from __future__ import annotations
from ..permissions import Intent, Operator


def deterministic_response(world, goal: str, active_house: str) -> dict:
    op = Operator(kind="owner", active_house=active_house, name="local-fallback")
    g = goal.lower()
    actions = []
    if "arm" in g and "night" in g:
        r = world.router.execute(Intent(active_house, "alarm", "panel", "arm", {"mode": "night"}), op)
        actions.append({"cmd": "arm night", "status": r.status, "message": r.message})
    elif "lights off" in g or "all lights off" in g:
        for name in ("living_room", "kitchen"):
            r = world.router.execute(Intent(active_house, "light", name, "turn_off"), op)
            actions.append({"cmd": f"{name} off", "status": r.status, "message": r.message})
    return {
        "mode": "fallback",
        "final": "AI advisor offline — local automations remain active; handled the goal deterministically where possible.",
        "actions": actions,
    }
