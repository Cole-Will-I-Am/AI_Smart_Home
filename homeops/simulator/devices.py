"""Device fault-injection helpers used by the fail-safe tests.

These set attribute flags the simulator honours, so a test can make a valve stall or a lock
jam and then assert that the *manual override* still works (human override is never gated).
"""
from __future__ import annotations
from ..state import StateStore


def inject_jam(state: StateStore, entity_id: str) -> None:
    state.entity(entity_id).attributes["jam"] = True


def inject_valve_stall(state: StateStore, entity_id: str) -> None:
    state.entity(entity_id).attributes["stall"] = True


def inject_generator_fail(state: StateStore, entity_id: str) -> None:
    state.entity(entity_id).attributes["fail_start"] = True


def clear_faults(state: StateStore, entity_id: str) -> None:
    for k in ("jam", "stall", "fail_start"):
        state.entity(entity_id).attributes.pop(k, None)
