from homeops.model import CANON


def test_two_houses_loaded(world):
    assert {"house_a", "house_b"} <= set(world.houses)


def test_canonical_entities_per_house(world):
    for hid in ("house_a", "house_b"):
        assert len(world.state.all_entities(hid)) == len(CANON)


def test_roles_and_approved_hardware(world):
    valve = world.state.entity("house_a.water.main_valve")
    assert valve.approved_hardware and valve.role == "main_water_valve"
    egress = world.state.entity("house_a.lock.egress_side")
    assert egress.role == "designated_egress_door"
    # thermostats are NOT approved hardware (so L3 mode changes on them are refused)
    assert not world.state.entity("house_a.climate.thermostat_main").approved_hardware
    # both houses are built from the same canonical template (transferability)
    assert set(e.role for e in world.state.all_entities("house_a")) == \
        set(e.role for e in world.state.all_entities("house_b"))
