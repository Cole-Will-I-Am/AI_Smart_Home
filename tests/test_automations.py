from homeops.events import Event
from homeops.simulator import scenarios


def test_leak_two_signal_shutoff(world):
    scenarios.leak(world, "house_a")
    world.tick(2)
    assert world.state.get_state("house_a.water.main_valve") == "closed"
    assert any(n["urgent"] for n in world.notifications)


def test_rogue_device_quarantined(world):
    scenarios.rogue_device(world, "house_a", "3c:6a:9d:aa:bb:cc")
    assert world.net.vlan_of("house_a", "3c:6a:9d:aa:bb:cc") == "iot_guest"


def test_fire_co_response(world):
    scenarios.fire_co(world, "house_a")
    assert world.state.get_state("house_a.lock.egress_side") == "unlocked"
    assert world.state.get_state("house_a.hvac.main") == "off"
    assert world.state.get_state("house_a.light.exterior_front") == "on"


def test_grid_failure_response(world):
    scenarios.grid_failure(world, "house_a")
    assert world.state.get_state("house_a.battery.main") == "backup"
    assert str(world.state.get_state("house_a.power.load_shed")).startswith("shedding")


def test_freeze_protection(world):
    scenarios.freeze_risk(world, "house_a")
    assert world.state.get_state("house_a.climate.thermostat_main") == 72


def test_intrusion_response(world):
    scenarios.intrusion(world, "house_a")
    assert world.state.get_state("house_a.lock.front_door") == "locked"
    assert world.state.get_state("house_a.light.exterior_front") == "on"


def test_high_power_response(world):
    scenarios.high_power(world, "house_a", 18000)
    assert world.state.get_state("house_a.evcharger.main") == 8
    assert any(r.status == "recommended" for r in world.audit.records)


def test_night_motion_response(world):
    scenarios.night_motion(world, "house_a")
    assert world.state.get_state("house_a.light.exterior_front") == "on"


def test_automation_scoped_to_one_house(world):
    scenarios.leak(world, "house_a")
    world.tick(2)
    # House B's valve is untouched — automations are house-scoped
    assert world.state.get_state("house_b.water.main_valve") == "open"


def test_malformed_flow_value_does_not_abort_and_fails_closed(world):
    world.state.set_state("house_a.sensor.leak_kitchen", "wet")
    world.state.set_state("house_a.sensor.flow_meter", "unknown")

    world.bus.publish(Event("leak", "house_a", "house_a.sensor.leak_kitchen", {}, world.engine.tick))

    assert world.state.get_state("house_a.water.main_valve") == "closing"
    assert any(n["urgent"] and "unreadable flow" in n["message"] for n in world.notifications)
    assert any(r.status == "executed" and r.subsystem == "water" and r.action == "shutoff_main"
               for r in world.audit.records)
