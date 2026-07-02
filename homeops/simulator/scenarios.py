"""Scenario injectors: raise the sensor/event conditions the emergency automations react to.

Each function publishes the same events real sensors/network gear would, so the local-first
automations fire exactly as they would in the field.
"""
from __future__ import annotations
from ..events import Event


def leak(world, house_id: str, sensor: str = "leak_kitchen", flow: float = 45.0) -> None:
    """Two-signal leak: a wet sensor AND abnormal flow."""
    ent = f"{house_id}.sensor.{sensor}"
    world.state.set_state(ent, "wet")
    world.state.set_state(f"{house_id}.sensor.flow_meter", flow)
    world.bus.publish(Event("leak", house_id, ent, {"flow": flow}, world.engine.tick))


def rogue_device(world, house_id: str, mac: str = "3c:6a:9d:aa:bb:cc") -> None:
    world.net.join(house_id, mac, "trusted")
    world.bus.publish(Event("network_join", house_id, None, {"mac": mac}, world.engine.tick))


def night_motion(world, house_id: str, zone: str = "front") -> None:
    world.state.houses[house_id].mode = "night"
    world.bus.publish(Event("motion", house_id, f"{house_id}.sensor.motion_{zone}", {"zone": zone}, world.engine.tick))


def high_power(world, house_id: str, watts: int = 18000) -> None:
    world.bus.publish(Event("power_draw", house_id, f"{house_id}.power.panel", {"watts": watts}, world.engine.tick))


def grid_failure(world, house_id: str) -> None:
    world.state.houses[house_id].grid_up = False
    world.bus.publish(Event("grid", house_id, None, {"status": "down"}, world.engine.tick))


def wan_failure(world, house_id: str) -> None:
    world.state.houses[house_id].wan_up = False
    world.bus.publish(Event("wan", house_id, None, {"status": "down"}, world.engine.tick))


def fire_co(world, house_id: str) -> None:
    world.state.set_state(f"{house_id}.sensor.smoke_co_hall", "verified")
    world.bus.publish(Event("smoke_co", house_id, f"{house_id}.sensor.smoke_co_hall", {"verified": True}, world.engine.tick))


def freeze_risk(world, house_id: str, zone: str = "garage", temp: int = 34) -> None:
    world.state.set_state(f"{house_id}.sensor.freeze_{zone}", temp)
    world.bus.publish(Event("temp", house_id, f"{house_id}.sensor.freeze_{zone}", {"temp": temp, "zone": zone}, world.engine.tick))


def intrusion(world, house_id: str) -> None:
    world.bus.publish(Event("perimeter", house_id, None, {"suspicious": True}, world.engine.tick))
