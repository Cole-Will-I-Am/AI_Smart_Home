"""House modes and their fail-safe defaults (DESIGN.md §Q).

Modes gate which AI actions are permitted and what the fail-safe state is. Kept small and
explicit so it is obvious what each mode changes.
"""
from __future__ import annotations

MODES = ("home", "away", "night", "vacation", "guest", "emergency")

# Fail-safe defaults chosen per subsystem (DESIGN.md §W):
#   egress doors  -> operable from inside regardless of power/AI (fail-safe)
#   perimeter     -> fail-secure (stay locked on fault)
#   loads         -> fail to manual (last state, manual handle works)
#   WAN inbound   -> fail-closed
FAILSAFE = {
    "designated_egress_door": "operable_from_inside",
    "exterior_door": "fail_secure",
    "noncritical_breaker": "fail_to_manual",
    "critical_breaker": "fail_to_manual",
}


def is_night_or_away(mode: str) -> bool:
    return mode in ("night", "away", "vacation")
