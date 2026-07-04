"""Vigilance tier: robust baselines, spike-proof learning, advisory-only response."""
from homeops.baseline import SLOTS, BaselineModel
from homeops.events import Event


def _train(model, entity="house_a.power.circuit_office", slot=10, weeks=30, base=300.0):
    # deterministic quasi-noise around the base value, no randomness
    for w in range(weeks):
        model.observe("house_a", entity, base + 10.0 * ((w * 7) % 5 - 2), slot)
    return entity, slot


def test_spike_flags_and_normal_does_not():
    m = BaselineModel(min_samples=24)
    entity, slot = _train(m)
    assert m.observe("house_a", entity, 305.0, slot) is None          # within normal
    a = m.observe("house_a", entity, 1500.0, slot)                    # 5x draw
    assert a is not None and a.z >= m.z_threshold and abs(a.expected - 300.0) <= 20.0


def test_min_samples_gate_no_verdict_from_thin_evidence():
    m = BaselineModel(min_samples=24)
    for w in range(10):                                               # only 10 samples
        m.observe("house_a", "e", 300.0, 5)
    assert m.observe("house_a", "e", 1500.0, 5) is None


def test_outliers_are_never_absorbed():
    # a sustained failure cannot teach the model that broken is normal
    m = BaselineModel(min_samples=24)
    entity, slot = _train(m)
    for _ in range(50):
        assert m.observe("house_a", entity, 1500.0, slot) is not None


def test_abs_floor_deadband_on_steady_signals():
    m = BaselineModel(min_samples=24, floors={"flow": 5.0})
    for w in range(30):
        m.observe("house_a", "flow", 0.0 + 0.001 * (w % 3), 3)        # near-constant
    assert m.observe("house_a", "flow", 2.0, 3) is None               # z huge, physically trivial
    assert m.observe("house_a", "flow", 30.0, 3) is not None          # material AND extreme


def test_slots_are_independent():
    m = BaselineModel(min_samples=24)
    _train(m, slot=10, base=300.0)
    # same entity, untrained slot -> no verdict (weekday rhythm != weekend rhythm)
    assert m.observe("house_a", "house_a.power.circuit_office", 1500.0, 11) is None


def test_persistence_roundtrip():
    m = BaselineModel(min_samples=24)
    entity, slot = _train(m)
    m2 = BaselineModel(min_samples=24)
    m2.load_dict(m.to_dict())
    assert m2.observe("house_a", entity, 1500.0, slot) is not None


def test_bus_integration_anomaly_is_advisory_only(world):
    """Telemetry in -> anomaly event + notification out -> and NOTHING actuates."""
    ent = "house_a.power.circuit_office"
    for w in range(30):                                               # train slot 10 across weeks
        world.bus.publish(Event("power_draw", "house_a", ent,
                                {"watts": 300.0 + 10.0 * ((w * 7) % 5 - 2)}, tick=10 + SLOTS * w))
    audit_before = len(world.audit.records)
    world.bus.publish(Event("power_draw", "house_a", ent, {"watts": 1500.0}, tick=10 + SLOTS * 31))
    anomalies = [e for e in world.bus.history if e.type == "anomaly"]
    assert anomalies and anomalies[-1].entity_id == ent
    notes = [n for n in world.notifications if "Anomaly" in n["message"]]
    assert notes, "vigilance tier must notify"
    # advisory only: no intent was executed in response (audit unchanged), valve untouched
    assert len(world.audit.records) == audit_before
    assert world.state.get_state("house_a.water.main_valve") == "open"
