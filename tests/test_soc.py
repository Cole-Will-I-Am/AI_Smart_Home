"""Part 13a: Home SOC — operational intelligence as pure reduction over engine streams.
The invariant under test is twofold: the analytics are correct, AND they never actuate
(a SOC that could act would be an authority outside the permission engine)."""
from homeops import soc
from homeops.permissions import Intent, Operator
from homeops.simulator import scenarios

OWNER = Operator("owner", "house_a", "resident")


# ---- readiness ---------------------------------------------------------------------
def test_clean_house_is_armed(world):
    rep = soc.readiness(world, "house_a")
    assert rep.armed and rep.score == 1.0


def test_leak_breaks_readiness_with_named_cause(world):
    scenarios.leak(world, "house_a")
    world.tick(3)
    rep = soc.readiness(world, "house_a")
    assert not rep.armed and rep.score < 1.0
    broken = {i.key for i in rep.items if not i.ready}
    assert "leak_mesh" in broken and "water_main" in broken     # the wet sensor and the closed main


def test_offline_safety_device_is_not_ready_even_if_state_looks_safe(world):
    # a lock reads "locked" but its device is offline -> readiness must fail closed
    lock = next(e for e in world.houses["house_a"].entities.values() if e.subsystem == "lock")
    world.health.mark_offline(lock.entity_id)
    rep = soc.readiness(world, "house_a")
    locks = next(i for i in rep.items if i.key == "locks")
    assert not locks.ready and "offline" in locks.detail


# ---- health drift ------------------------------------------------------------------
def test_drift_orders_offline_first(world):
    ents = list(world.houses["house_a"].entities.values())
    world.health.heartbeat(ents[0].entity_id, world.engine.tick)
    world.health.mark_offline(ents[1].entity_id)
    drift = soc.health_drift(world, "house_a")
    assert drift[0].status == "offline"


def test_drifting_catches_devices_before_they_go_stale(world):
    e = next(iter(world.houses["house_a"].entities.values()))
    win = world.health.window
    world.health.heartbeat(e.entity_id, world.engine.tick)
    world.engine.tick += int(win * 0.8)                          # 80% through the window, still "ok"
    d = soc.drifting(world, "house_a", threshold=0.66)
    assert any(x.entity_id == e.entity_id for x in d)
    assert soc.health_drift(world, "house_a")                    # status still ok, just drifting
    assert world.health.status(e.entity_id, world.engine.tick) == "ok"


# ---- correlation -------------------------------------------------------------------
def test_correlate_clusters_an_incident_not_singletons(world):
    # one multi-action incident (intrusion: light + camera) ...
    scenarios.intrusion(world, "house_a")
    world.tick(2)
    incidents = soc.correlate(world, "house_a")
    assert incidents
    biggest = max(incidents, key=lambda i: len(i.records))
    assert len(biggest.records) >= 2 and len(biggest.subsystems) >= 2


def test_correlate_flags_refusals(world):
    world.router.execute(Intent("house_a", "lock", "front_door", "unlock_unknown"), OWNER)  # L4 recommend-only
    incidents = soc.correlate(world, "house_a", interesting_only=False)
    assert any(i.had_refusal for i in incidents)


def test_correlate_separates_distant_events(world):
    scenarios.leak(world, "house_a")
    world.tick(1)
    world.engine.tick += 10                                      # a gap wider than the window
    scenarios.intrusion(world, "house_a")
    world.tick(1)
    incidents = soc.correlate(world, "house_a", window=2)
    assert len(incidents) >= 2                                   # not merged into one


# ---- overnight diff ----------------------------------------------------------------
def test_overnight_diff_flags_safety_changes(world):
    before = soc.snapshot(world, "house_a")
    scenarios.leak(world, "house_a")
    world.tick(3)
    deltas = soc.overnight_diff(before, soc.snapshot(world, "house_a"), world, "house_a")
    valve = next(d for d in deltas if d.entity_id.endswith("water.main_valve"))
    assert valve.safety_relevant and valve.before != valve.after


def test_overnight_diff_empty_when_nothing_changes(world):
    snap = soc.snapshot(world, "house_a")
    assert soc.overnight_diff(snap, dict(snap)) == []


# ---- the invariant: SOC is read-only -----------------------------------------------
def test_soc_never_actuates(world):
    """Run every analytic and assert not one audit record was written and no state moved."""
    before_state = soc.snapshot(world, "house_a")
    n_audit = len(world.audit.records)
    soc.readiness(world, "house_a")
    soc.health_drift(world, "house_a")
    soc.drifting(world, "house_a")
    soc.correlate(world, "house_a")
    soc.situation_report(world, "house_a", prior_snapshot=before_state)
    assert len(world.audit.records) == n_audit                  # no writes
    assert soc.snapshot(world, "house_a") == before_state       # no state change


def test_situation_report_composites_everything(world):
    before = soc.snapshot(world, "house_a")
    scenarios.leak(world, "house_a")
    world.tick(3)
    rep = soc.situation_report(world, "house_a", prior_snapshot=before)
    assert rep["armed"] is False
    assert rep["audit_intact"] is True
    assert any(sr[0] == "leak_mesh" and sr[1] is False for sr in rep["readiness"])
    assert rep["overnight_changes"]
