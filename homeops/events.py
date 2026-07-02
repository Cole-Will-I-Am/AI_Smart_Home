"""Minimal synchronous event bus + typed events.

Synchronous + tick-based on purpose: deterministic, trivially testable, no event-loop
timing to flake on. Real HA would push events over a WebSocket; the automations code is
identical either way because it only sees `Event` objects.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    type: str                 # leak | network_join | motion | power_draw | grid | wan | smoke_co | temp | perimeter | state
    house_id: str
    entity_id: str | None = None
    data: dict = field(default_factory=dict)
    tick: int = 0


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Callable[[Event], None]] = []
        self.history: list[Event] = []

    def subscribe(self, handler: Callable[[Event], None]) -> None:
        self._subs.append(handler)

    def publish(self, event: Event) -> None:
        self.history.append(event)
        for h in list(self._subs):
            h(event)

    def recent(self, n: int = 20, house_id: str | None = None) -> list[Event]:
        evs = [e for e in self.history if house_id is None or e.house_id == house_id]
        return evs[-n:]
