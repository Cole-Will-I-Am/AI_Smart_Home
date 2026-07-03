"""Deterministic local fallback used when the Claude API / internet is unavailable.

The house never depends on the AI: the local-first automations keep running on the event bus.
This fallback only covers *interactive operator goals* when the advisor is unreachable — it maps
a few obvious goals to engine-validated commands and otherwise reports that the advisory is
offline while locals stay active.
"""
from __future__ import annotations
from ..permissions import Intent, Operator


def deterministic_response(world, goal: str, active_house: str) -> dict:
    # Runs as an AI-limited operator, NOT owner — the AI layer must never silently elevate
    # authority when it degrades to fallback. L2+ goals therefore return confirm_required
    # (a human must confirm), exactly as on the online AI path.
    op = Operator(kind="ai", active_house=active_house, name="local-fallback")
    g = goal.lower()
    actions = []
    def _do(subsystem, target, action, args=None, label=""):
        intent = Intent(active_house, subsystem, target, action, args or {})
        r = world.router.execute(intent, op)
        actions.append({"cmd": label or f"{subsystem}.{target} {action}", "status": r.status,
                        "message": r.message, "level": r.level,
                        "intent": {"house_id": active_house, "subsystem": subsystem,
                                   "target": target, "action": action, "args": args or {}}})
    if "arm" in g and "night" in g:
        _do("alarm", "panel", "arm", {"mode": "night"}, "arm night")
    elif "lights off" in g or "all lights off" in g:
        for name in ("living_room", "kitchen"):
            _do("light", name, "turn_off", label=f"{name} off")
    return {
        "mode": "fallback",
        "final": "AI advisor offline — local automations remain active; handled the goal deterministically where possible.",
        "actions": actions,
    }
