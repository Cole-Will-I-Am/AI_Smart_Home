"""Portfolio view — the family-office / estate-manager "one pane of glass" over N properties.

The engine is already N-property (everything keys off house_id); this adds the aggregation the
managed tier sells: per-property mode/connectivity/health/safety snapshot plus portfolio rollups,
including a live audit-integrity check across the whole estate.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Portfolio:
    id: str
    name: str
    property_ids: list[str] = field(default_factory=list)


SAFETY_SUBSYSTEMS = ("lock", "water", "alarm", "generator")


def portfolio_summary(world, property_ids: list[str] | None = None) -> dict:
    ids = property_ids or list(world.houses)
    now = world.engine.tick
    props: dict[str, dict] = {}
    total_offline = 0
    total_urgent = 0
    for hid in ids:
        h = world.houses[hid]
        offline = [e.entity_id for e in h.entities.values() if world.health.status(e.entity_id, now) == "offline"]
        urgents = [n for n in world.notifications if n["house_id"] == hid and n["urgent"]]
        safety = {e.name: e.state for e in h.entities.values() if e.subsystem in SAFETY_SUBSYSTEMS}
        props[hid] = {
            "alias": h.alias, "mode": h.mode, "wan_up": h.wan_up, "grid_up": h.grid_up,
            "ai_hold": h.ai_hold, "offline_devices": offline, "urgent_alerts": len(urgents),
            "safety": safety,
        }
        total_offline += len(offline)
        total_urgent += len(urgents)
    audit_ok, _ = world.audit.verify_chain()
    return {
        "n_properties": len(ids),
        "total_offline_devices": total_offline,
        "total_urgent_alerts": total_urgent,
        "audit_intact": audit_ok,
        "properties": props,
    }
