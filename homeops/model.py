"""Canonical device/entity model, built from config/houses.example.yaml.

The example YAML is a human-facing *description*; `build_house` normalizes it into a
guaranteed canonical entity set so the simulator, permission engine, and automations
always have the entities they reference — regardless of how sparse the YAML is. Real
device IDs from the YAML are read where present; sensible defaults fill the rest. Both
houses are built from the SAME canonical spec (role-based) — that is what makes House B
a parameterized copy of House A (DESIGN.md §S Transfer plan).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import yaml

# (subsystem, name, role, approved_hardware, initial_state)
CANON: list[tuple[str, str, str, bool, Any]] = [
    ("light", "living_room", "interior_lighting", False, "off"),
    ("light", "kitchen", "interior_lighting", False, "off"),
    ("light", "exterior_front", "exterior_lighting", False, "off"),
    ("light", "exterior_back", "exterior_lighting", False, "off"),
    ("climate", "thermostat_main", "thermostat", False, 68),
    ("climate", "thermostat_up", "thermostat", False, 68),
    ("cover", "blinds_living", "shades", False, "open"),
    ("plug", "noncritical_1", "noncritical_plug", False, "off"),
    ("speaker", "intercom", "intercom_audio", False, "idle"),
    ("lock", "front_door", "exterior_door", False, "locked"),
    ("lock", "back_door", "exterior_door", False, "locked"),
    ("lock", "egress_side", "designated_egress_door", False, "locked"),
    ("garage", "main", "garage_door", False, "closed"),
    ("camera", "front_door", "camera", False, "idle"),
    ("camera", "driveway", "camera", False, "idle"),
    ("camera", "back_yard", "camera", False, "idle"),
    ("sensor", "leak_kitchen", "leak_sensor", False, "dry"),
    ("sensor", "leak_bath", "leak_sensor", False, "dry"),
    ("sensor", "leak_basement", "leak_sensor", False, "dry"),
    ("sensor", "motion_front", "motion", False, "clear"),
    ("sensor", "contact_front", "contact", False, "closed"),
    ("sensor", "glassbreak_living", "glass_break", False, "quiet"),
    ("sensor", "smoke_co_hall", "smoke_co", False, "clear"),   # observe-only for AI
    ("sensor", "temp_basement", "temp", False, 55),
    ("sensor", "flow_meter", "flow_meter", False, 0.0),
    ("sensor", "pressure", "pressure", False, 60),
    ("sensor", "freeze_garage", "freeze", False, 40),
    ("water", "main_valve", "main_water_valve", True, "open"),
    ("water", "irrigation", "irrigation", False, "off"),
    ("hvac", "main", "hvac", True, "circulating"),
    ("power", "panel", "smart_panel", True, "nominal"),
    ("power", "breaker_ev", "noncritical_breaker", True, "on"),
    ("power", "breaker_furnace", "critical_breaker", True, "on"),
    ("power", "load_shed", "load_shed", True, "idle"),
    ("evcharger", "main", "evcharger", True, 32),
    ("battery", "main", "battery_backup", True, "grid"),
    ("generator", "main", "generator", True, "off"),
    ("alarm", "panel", "alarm_panel", False, "disarmed"),
    ("network", "firewall", "firewall", True, "active"),
]

SUBSYS_ACTIONS: dict[str, list[str]] = {
    "light": ["turn_on", "turn_off", "set_brightness"],
    "climate": ["set_temperature", "set_fan", "set_mode"],
    "cover": ["open", "close", "set_position"],
    "plug": ["turn_on", "turn_off"],
    "speaker": ["announce"],
    "lock": ["lock", "unlock"],
    "garage": ["open", "close"],
    "camera": ["set_mode", "snapshot", "export"],
    "sensor": [],  # observe-only
    "water": ["shutoff_main", "open_main", "irrigation_on", "irrigation_off"],
    "hvac": ["emergency_shutoff"],
    "power": ["breaker_on", "breaker_off", "load_shed", "main_breaker"],
    "evcharger": ["set_limit"],
    "battery": ["set_mode"],
    "generator": ["start"],
    "alarm": ["arm", "disarm", "escalate", "disable"],
    "network": ["quarantine", "firewall_policy", "firewall_restructure"],
}


@dataclass
class Entity:
    house_id: str
    subsystem: str
    name: str
    role: str
    approved_hardware: bool
    state: Any
    attributes: dict = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)

    @property
    def entity_id(self) -> str:
        return f"{self.house_id}.{self.subsystem}.{self.name}"


@dataclass
class House:
    id: str
    alias: str
    supernet: str
    rooms: list[str]
    zones: dict
    entities: dict[str, Entity]
    network_segments: dict
    backup_profile: dict
    # runtime flags
    mode: str = "home"          # home | away | night | vacation | guest | emergency
    wan_up: bool = True
    grid_up: bool = True
    ai_hold: bool = False       # per-house "AI hold": suspend AI actuation, locals keep running


def build_house(house_id: str, cfg: dict) -> House:
    entities: dict[str, Entity] = {}
    for subsystem, name, role, approved, init in CANON:
        e = Entity(
            house_id=house_id, subsystem=subsystem, name=name, role=role,
            approved_hardware=approved, state=init,
            actions=list(SUBSYS_ACTIONS.get(subsystem, [])),
            attributes={"ai_access": "observe" if role == "smoke_co" else "control"},
        )
        # thermostats carry an approved range for L1 in-range checks
        if subsystem == "climate":
            e.attributes["min_f"], e.attributes["max_f"] = 60, 82
        entities[e.entity_id] = e
    return House(
        id=house_id,
        alias=cfg.get("alias", house_id),
        supernet=cfg.get("supernet", ""),
        rooms=cfg.get("rooms", []),
        zones=cfg.get("zones", {}),
        entities=entities,
        network_segments=cfg.get("network_segments", {}),
        backup_profile=cfg.get("backup_profile", {}),
    )


def load_houses(path: str) -> dict[str, House]:
    with open(path) as f:
        doc = yaml.safe_load(f)
    houses_cfg = doc.get("houses", {})
    return {hid: build_house(hid, cfg or {}) for hid, cfg in houses_cfg.items()}
