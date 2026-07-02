from homeops.permissions import Intent, Operator


def owner():
    return Operator("owner", "house_a")


def test_rate_limit(bare):
    statuses = [bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner()).status
                for _ in range(8)]
    assert statuses.count("executed") <= 5
    assert any(r.status == "refused" and "rate" in r.message for r in bare.audit.records)


def test_rollback(bare):
    r = bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner())
    assert bare.state.get_state("house_a.light.living_room") == "on" and r.rollback_token
    assert bare.router.rollback(r.rollback_token) is True
    assert bare.state.get_state("house_a.light.living_room") == "off"


def test_valve_takes_ticks_to_close(bare):
    r1 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main"), owner())
    r2 = bare.router.execute(
        Intent("house_a", "water", "main_valve", "shutoff_main", confirm_token=r1.confirm_token), owner())
    assert r2.status == "executed"
    assert bare.state.get_state("house_a.water.main_valve") == "closing"
    bare.tick(2)
    assert bare.state.get_state("house_a.water.main_valve") == "closed"


def test_in_range_thermostat_clamped(bare):
    r = bare.router.execute(
        Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 200}), owner())
    assert r.status == "executed"
    assert bare.state.get_state("house_a.climate.thermostat_main") == 82   # clamped to approved max
