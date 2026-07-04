"""Part 13b: the estate digital twin — one derived model of structure + state + authority + risk.
Invariants: the twin is READ-ONLY and rebuildable; risk is a transparent, declared function;
authority levels come from the real engine, not a copy."""
from homeops import soc
from homeops.twin import CRITICALITY, EstateTwin
from homeops.simulator import scenarios


def test_twin_covers_every_entity(world):
    t = EstateTwin(world)
    n_entities = sum(len(h.entities) for h in world.houses.values())
    assert len(t.devices) == n_entities


def test_authority_levels_come_from_the_engine(world):
    t = EstateTwin(world)
    # a lock's primary action (unlock) is L2 in the engine; the twin must agree, not guess
    lock = next(d for d in t.devices.values() if d.subsystem == "lock")
    assert lock.authority_level == world.engine.level("lock", "unlock")
    water = next(d for d in t.devices.values() if d.subsystem == "water" and "main" in d.name)
    assert water.authority_level == world.engine.level("water", "shutoff_main")


def test_risk_rises_with_exposure_then_health(world):
    t0 = EstateTwin(world)
    leak0 = next(d for d in t0.devices.values() if d.name == "leak_kitchen")
    base = leak0.risk

    scenarios.leak(world, "house_a")
    world.tick(1)  # sensor now 'wet' -> exposure 1.0
    t1 = EstateTwin(world)
    leak1 = next(d for d in t1.devices.values() if d.name == "leak_kitchen")
    assert leak1.risk > base                                    # exposure raised risk

    world.health.mark_offline(leak1.entity_id)                 # now also unhealthy
    t2 = EstateTwin(world)
    leak2 = next(d for d in t2.devices.values() if d.name == "leak_kitchen")
    assert leak2.risk >= leak1.risk                            # health penalty (capped at 1.0)


def test_room_and_house_risk_track_worst_device(world):
    scenarios.leak(world, "house_a")
    world.tick(1)
    t = EstateTwin(world)
    kitchen = next(r for r in t.rooms("house_a") if r.name == "kitchen")
    assert kitchen.risk_profile in ("elevated", "critical")
    assert t.house_risk("house_a") >= kitchen.risk - 1e-9      # house >= any room


def test_top_risks_are_sorted_and_bounded(world):
    scenarios.leak(world, "house_a")
    world.tick(1)
    t = EstateTwin(world)
    top = t.top_risks("house_a", 5)
    assert top == sorted(top, key=lambda d: d.risk, reverse=True)
    assert all(0.0 <= d.risk <= 1.0 for d in t.devices.values())
    assert top[0].name == "leak_kitchen"                        # the active leak is #1


def test_would_authorize_matches_router_gate(world):
    t = EstateTwin(world)
    # the twin's cheap preview must equal the engine's real level for the same action
    for sub, act in [("light", "turn_on"), ("lock", "unlock"),
                     ("water", "shutoff_main"), ("alarm", "disable_smoke_co")]:
        assert t.would_authorize(sub, act) == world.engine.level(sub, act)


def test_risk_model_is_transparent_not_learned():
    # the point of the declared table: a consultant can read and tune it
    assert CRITICALITY["sensor"] == 1.0 and CRITICALITY["light"] < CRITICALITY["water"]


def test_twin_is_readonly_and_rebuildable(world):
    before = soc.snapshot(world, "house_a")
    n_audit = len(world.audit.records)
    t1 = EstateTwin(world)
    _ = t1.to_dict("house_a")
    t1.top_risks("house_a")
    t1.rooms_by_risk("house_a")
    t1.would_authorize("lock", "unlock")
    assert soc.snapshot(world, "house_a") == before            # observing didn't mutate the world
    assert len(world.audit.records) == n_audit
    # rebuildable: a fresh twin from the same world is equivalent
    t2 = EstateTwin(world)
    assert t2.to_dict("house_a") == t1.to_dict("house_a")


def test_to_dict_is_serializable_and_ordered(world):
    import json
    scenarios.leak(world, "house_a")
    world.tick(1)
    d = EstateTwin(world).to_dict("house_a")
    json.dumps(d)                                              # must not raise
    rooms = d["rooms"]
    assert rooms == sorted(rooms, key=lambda r: r["risk"], reverse=True)
    assert d["house_risk"] >= max(r["risk"] for r in rooms) - 1e-9
