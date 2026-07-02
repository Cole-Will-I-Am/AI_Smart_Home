from homeops.simulator import devices, scenarios
from homeops.permissions import Intent, Operator


def owner():
    return Operator("owner", "house_a")


def test_lock_jam_then_manual_override(bare):
    devices.inject_jam(bare.state, "house_a.lock.front_door")
    r = bare.router.execute(Intent("house_a", "lock", "front_door", "lock"), owner())
    assert r.status == "refused" and "JAM" in r.message
    # human override always works, regardless of the engine
    bare.state.manual_override("house_a.lock.front_door", "locked")
    assert bare.state.get_state("house_a.lock.front_door") == "locked"


def test_valve_stall_then_manual_override(world):
    devices.inject_valve_stall(world.state, "house_a.water.main_valve")
    scenarios.leak(world, "house_a")
    world.tick(2)
    assert world.state.get_state("house_a.water.main_valve") == "closing"   # stuck
    world.state.manual_override("house_a.water.main_valve", "closed")
    assert world.state.get_state("house_a.water.main_valve") == "closed"


def test_generator_fail_then_manual_start(bare):
    devices.inject_generator_fail(bare.state, "house_a.generator.main")
    r1 = bare.router.execute(Intent("house_a", "generator", "main", "start"), owner())
    r2 = bare.router.execute(
        Intent("house_a", "generator", "main", "start", confirm_token=r1.confirm_token), owner())
    # a generator that fails to start is UNVERIFIED, not "executed" — read-back caught it
    assert r2.status == "unverified"
    assert bare.state.get_state("house_a.generator.main") == "failed"
    bare.state.manual_override("house_a.generator.main", "running")
    assert bare.state.get_state("house_a.generator.main") == "running"
