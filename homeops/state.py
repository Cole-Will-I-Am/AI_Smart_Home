"""StateStore — the HA-like state layer over both houses.

Holds every entity's current state + attributes, keyed by the fully-qualified,
house-namespaced entity id (e.g. `house_a.lock.front_door`). `manual_override` models the
physical control that always works regardless of the engine/AI/power state.
"""
from __future__ import annotations
from typing import Any, Callable
from .model import House, Entity
from .audit import AuditLog, AuditRecord


class StateStore:
    def __init__(self, houses: dict[str, House], audit: AuditLog | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self.houses = houses
        self.audit = audit           # if set, manual overrides are recorded (audit completeness)
        self.clock = clock or (lambda: 0)

    def entity(self, entity_id: str) -> Entity | None:
        hid = entity_id.split(".", 1)[0]
        house = self.houses.get(hid)
        return house.entities.get(entity_id) if house else None

    def get_state(self, entity_id: str) -> Any:
        e = self.entity(entity_id)
        return e.state if e else None

    def set_state(self, entity_id: str, state: Any, **attrs: Any) -> None:
        e = self.entity(entity_id)
        if not e:
            raise KeyError(entity_id)
        e.state = state
        e.attributes.update(attrs)

    def manual_override(self, entity_id: str, state: Any) -> None:
        """Physical override: always succeeds, bypasses the engine entirely — and is audited."""
        e = self.entity(entity_id)
        if not e:
            raise KeyError(entity_id)
        e.state = state
        e.attributes["manual"] = True
        if self.audit:
            parts = entity_id.split(".")
            self.audit.record(AuditRecord(
                tick=self.clock(), operator="human", house_id=parts[0],
                subsystem=parts[1] if len(parts) > 1 else "?", target=parts[2] if len(parts) > 2 else "?",
                action="manual_override", args={"state": state}, level=None,
                status="manual_override", message=f"physical override -> {state}"))

    def house(self, house_id: str) -> House:
        return self.houses[house_id]

    def all_entities(self, house_id: str | None = None) -> list[Entity]:
        out: list[Entity] = []
        for hid, house in self.houses.items():
            if house_id and hid != house_id:
                continue
            out.extend(house.entities.values())
        return out
