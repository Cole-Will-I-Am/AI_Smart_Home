"""Estate digital twin — one queryable model unifying structure, state, authority, and risk.

The SOC (soc.py) answers "what is happening"; the twin answers "what IS this estate" — a single
structured object in which every device carries its live state, its health, the authority level
required to actuate it, and a derived risk weight, and every room and subsystem rolls those up.
It is the substrate the brief calls "Palantir/Datadog/ServiceNow for a residence": simulate before
actuation, reason about risk per room, expose one structure instead of five scattered ones.

Like the SOC, the twin is READ-ONLY and derived — it observes the world, holds no authority, and
can be rebuilt from the world at any tick. Building a twin never changes anything.

Risk model (transparent by construction, so it can be argued with rather than trusted blindly):
each entity's risk weight = criticality(subsystem) × exposure(state) × health_penalty. The weights
are declared constants below, not learned — an estate-security consultant can inspect and tune them.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from .permissions import PermissionEngine

# Criticality: how much does this subsystem matter to life-safety / property protection? [0..1]
CRITICALITY: dict[str, float] = {
    "sensor": 1.0, "water": 0.95, "alarm": 0.95, "lock": 0.85, "generator": 0.8,
    "power": 0.8, "hvac": 0.6, "climate": 0.5, "battery": 0.6, "evcharger": 0.4,
    "garage": 0.5, "camera": 0.6, "network": 0.7, "speaker": 0.1, "light": 0.2,
    "plug": 0.2, "scene": 0.1,
}

# Exposure: does the CURRENT state represent an active or elevated-risk condition? [0..1]
# Keyed by (subsystem, state); default 0.1 baseline for "nominal".
EXPOSURE: dict[tuple, float] = {
    ("water", "closed"): 0.7, ("water", "closing"): 0.6,          # main shut = something's wrong
    ("lock", "unlocked"): 0.6,
    ("alarm", "triggered"): 1.0, ("alarm", "disarmed"): 0.4,
    ("sensor", "verified"): 1.0, ("sensor", "wet"): 1.0, ("sensor", "freezing"): 0.9,
    ("generator", "running"): 0.5, ("generator", "fault"): 1.0,
    ("garage", "open"): 0.5,
    ("hvac", "off"): 0.4,
}


def _exposure(subsystem: str, state) -> float:
    s = str(state).lower()
    if (subsystem, s) in EXPOSURE:
        return EXPOSURE[(subsystem, s)]
    # numeric sensor extremes (e.g. freeze temps) get mild exposure
    return 0.1


@dataclass
class DeviceTwin:
    entity_id: str
    subsystem: str
    name: str
    room: str
    state: object
    health: str                 # ok | stale | offline | unknown
    authority_level: int | None  # L to actuate its primary action; None if unknown
    approved_hardware: bool
    risk: float                 # derived, [0..~1]

    @property
    def is_safety(self) -> bool:
        return self.subsystem in ("sensor", "water", "alarm", "lock", "generator")


@dataclass
class RoomTwin:
    name: str
    devices: list[DeviceTwin] = field(default_factory=list)

    @property
    def risk(self) -> float:
        return round(max((d.risk for d in self.devices), default=0.0), 3)

    @property
    def risk_profile(self) -> str:
        r = self.risk
        return "critical" if r >= 0.7 else "elevated" if r >= 0.4 else "nominal"


@dataclass
class SubsystemTwin:
    name: str
    devices: list[DeviceTwin] = field(default_factory=list)

    @property
    def worst_health(self) -> str:
        order = ["offline", "stale", "unknown", "ok"]
        return next((s for s in order if any(d.health == s for d in self.devices)), "ok")

    @property
    def risk(self) -> float:
        return round(max((d.risk for d in self.devices), default=0.0), 3)


# primary action per subsystem, used to look up the authority level a device demands
PRIMARY_ACTION: dict[str, str] = {
    "light": "turn_on", "plug": "turn_on", "lock": "unlock", "garage": "open",
    "climate": "set_temp", "hvac": "emergency_off", "water": "shutoff_main",
    "power": "breaker_off", "generator": "start", "battery": "set_mode",
    "evcharger": "set_limit", "alarm": "disarm", "camera": "set_mode",
    "scene": "activate", "speaker": "announce", "sensor": None,
}


class EstateTwin:
    """A derived, queryable model of one estate at the current tick."""

    def __init__(self, world) -> None:
        self.world = world
        self.engine: PermissionEngine = world.engine
        self.devices: dict[str, DeviceTwin] = {}
        self.houses: dict[str, dict] = {}
        self._build()

    def _authority_level(self, subsystem: str) -> int | None:
        act = PRIMARY_ACTION.get(subsystem)
        if act is None:
            return 0            # observe-only subsystems sit at L0
        return self.engine.level(subsystem, act)

    def _build(self) -> None:
        now = self.engine.tick
        for hid, h in self.world.houses.items():
            rooms: dict[str, RoomTwin] = {}
            subs: dict[str, SubsystemTwin] = {}
            for e in h.entities.values():
                health = self.world.health.status(e.entity_id, now)
                level = self._authority_level(e.subsystem)
                crit = CRITICALITY.get(e.subsystem, 0.3)
                expo = _exposure(e.subsystem, e.state)
                pen = {"offline": 1.5, "stale": 1.2, "unknown": 1.1, "ok": 1.0}[health]
                risk = round(min(crit * max(expo, 0.1) * pen, 1.0), 3)
                room = e.attributes.get("room") or self._infer_room(e, h)
                dt = DeviceTwin(e.entity_id, e.subsystem, e.name, room, e.state, health,
                                level, e.approved_hardware, risk)
                self.devices[e.entity_id] = dt
                rooms.setdefault(room, RoomTwin(room)).devices.append(dt)
                subs.setdefault(e.subsystem, SubsystemTwin(e.subsystem)).devices.append(dt)
            self.houses[hid] = {"alias": h.alias, "mode": h.mode, "rooms": rooms, "subsystems": subs}

    @staticmethod
    def _infer_room(entity, house) -> str:
        for room in house.rooms:
            if room in entity.name:
                return room
        return "unassigned"

    # ---- queries --------------------------------------------------------------------
    def device(self, entity_id: str) -> DeviceTwin | None:
        return self.devices.get(entity_id)

    def rooms(self, house_id: str) -> list[RoomTwin]:
        return list(self.houses[house_id]["rooms"].values())

    def subsystems(self, house_id: str) -> list[SubsystemTwin]:
        return list(self.houses[house_id]["subsystems"].values())

    def top_risks(self, house_id: str | None = None, n: int = 5) -> list[DeviceTwin]:
        pool = [d for eid, d in self.devices.items()
                if house_id is None or eid.startswith(house_id + ".")]
        return sorted(pool, key=lambda d: d.risk, reverse=True)[:n]

    def rooms_by_risk(self, house_id: str) -> list[RoomTwin]:
        return sorted(self.rooms(house_id), key=lambda r: r.risk, reverse=True)

    def would_authorize(self, subsystem: str, action: str) -> int | None:
        """Simulate-before-actuate, at the authority layer: what level does this action demand?
        (The full router still governs actual execution; this is the twin's cheap preview.)"""
        return self.engine.level(subsystem, action)

    def house_risk(self, house_id: str) -> float:
        ds = [d for eid, d in self.devices.items() if eid.startswith(house_id + ".")]
        return round(max((d.risk for d in ds), default=0.0), 3)

    def to_dict(self, house_id: str) -> dict:
        """Serializable projection for an API / twin viewer."""
        return {
            "house_id": house_id,
            "alias": self.houses[house_id]["alias"],
            "mode": self.houses[house_id]["mode"],
            "house_risk": self.house_risk(house_id),
            "rooms": [{"room": r.name, "risk": r.risk, "profile": r.risk_profile,
                       "devices": [d.entity_id for d in r.devices]}
                      for r in self.rooms_by_risk(house_id)],
            "subsystems": [{"subsystem": s.name, "risk": s.risk, "health": s.worst_health,
                            "authority_level": self._authority_level(s.name)}
                           for s in self.subsystems(house_id)],
            "top_risks": [{"entity": d.entity_id, "state": d.state, "risk": d.risk,
                           "health": d.health, "L": d.authority_level}
                          for d in self.top_risks(house_id)],
        }
