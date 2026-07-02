"""Part 2 — verified actuation + device health."""
from homeops.permissions import Intent, Operator
from homeops.simulator import devices


def owner():
    return Operator("owner", "house_a")


def test_safety_critical_refused_on_offline_device(world):
    world.health.mark_offline("house_a.lock.front_door")
    r = world.router.execute(Intent("house_a", "lock", "front_door", "lock"), owner())
    assert r.status == "refused" and "offline" in r.message


def test_safety_critical_allowed_when_healthy(world):
    r = world.router.execute(Intent("house_a", "lock", "front_door", "lock"), owner())
    assert r.status == "executed"


def test_stale_device_refused_then_heartbeat_recovers(world):
    world.tick(world.health.window + 1)   # no heartbeat for the lock -> stale
    r = world.router.execute(Intent("house_a", "lock", "front_door", "lock"), owner())
    assert r.status == "refused" and "stale" in r.message
    world.health.heartbeat("house_a.lock.front_door", world.engine.tick)
    r2 = world.router.execute(Intent("house_a", "lock", "front_door", "lock"), owner())
    assert r2.status == "executed"


def test_unresponsive_device_is_unverified(world):
    op = owner()
    r1 = world.router.execute(Intent("house_a", "lock", "front_door", "unlock"), op)
    world.router.execute(Intent("house_a", "lock", "front_door", "unlock", confirm_token=r1.confirm_token), op)
    assert world.state.get_state("house_a.lock.front_door") == "unlocked"
    devices.inject_unresponsive(world.state, "house_a.lock.front_door")
    r = world.router.execute(Intent("house_a", "lock", "front_door", "lock"), op)
    assert r.status == "unverified"
    assert world.state.get_state("house_a.lock.front_door") == "unlocked"   # never actually moved


def test_non_safety_action_is_not_health_gated(world):
    world.health.mark_offline("house_a.light.living_room")
    r = world.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner())
    assert r.status == "executed"   # the health gate applies only to safety-critical actuation
