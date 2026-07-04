"""Per-device health / heartbeat registry.

A safety system must not command a device it cannot confirm is present and responsive. This tracks
when each entity was last seen (actuated or reported) and lets the router refuse safety-critical
actuation on a device that is offline or stale ("I can't verify the valve responds, so I won't claim
I closed it"). It's the enabling half of "verified actuation" — the other half is post-actuation
read-back (router verification for the sim, adapter read-back for real Home Assistant).
"""
from __future__ import annotations

DEFAULT_WINDOW = 30   # ticks; longer than any normal multi-tick operation


class HealthRegistry:
    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self.window = window
        self._last_seen: dict[str, int] = {}
        self._offline: set[str] = set()

    def heartbeat(self, entity_id: str, now: int) -> None:
        self._offline.discard(entity_id)
        self._last_seen[entity_id] = now

    def mark_offline(self, entity_id: str) -> None:
        self._offline.add(entity_id)

    def mark_unknown(self, entity_id: str) -> None:
        self._offline.discard(entity_id)
        self._last_seen.pop(entity_id, None)

    def status(self, entity_id: str, now: int) -> str:
        if entity_id in self._offline:
            return "offline"
        last = self._last_seen.get(entity_id)
        if last is None:
            return "unknown"
        return "ok" if (now - last) <= self.window else "stale"

    def healthy(self, entity_id: str, now: int) -> bool:
        # Unknown devices have not produced a heartbeat/preflight and fail closed.
        return self.status(entity_id, now) == "ok"
