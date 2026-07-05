"""Tests for the prognostics tier: remaining-useful-life estimation over the home's telemetry.

Like the other derived tiers it is a read-only reducer that never actuates. These tests pin
that, plus the two behaviours that make it honest: it produces a calibrated RUL when a real
trend exists, and it REFUSES (reject-option) when the signal is flat or noise-only.
"""
import pytest

from homeops import build_world
from homeops.prognostics import MIN_SAMPLES


@pytest.fixture
def w():
    return build_world()


def _drive(w, rel, start, step, n):
    """Feed a linear trend into a sensor, one tick per step, and return the final value."""
    val = start
    for _ in range(n):
        val += step
        w.state.set_state(f"house_a.{rel}", val)
        w.tick()
    return val


# ------------------------------------------------------------------ reject-option / honesty

def test_flat_signal_rejects(w):
    """A healthy, flat house yields no prognosis — every asset reports insufficient signal."""
    w.tick(50)   # sim telemetry is flat; filters see no trend
    statuses = {v["status"] for v in w.prognostics.latest.values()}
    assert statuses <= {"insufficient_signal"}
    assert not any(v["status"] == "degrading" for v in w.prognostics.latest.values())


def test_pure_noise_does_not_trigger(w):
    """Zero-mean noise with no drift must not be mistaken for degradation (reject-option)."""
    import random
    rng = random.Random(0)
    for _ in range(90):
        w.state.set_state("house_a.sensor.pressure", 60.0 + rng.gauss(0, 0.4))
        w.tick()
    prog = w.prognostics.latest[("house_a", "sensor.pressure")]
    # noise must never reach an actionable "degrading" call (the operationally-critical property);
    # with SNR_MIN=3 it also stays out of "healthy_trend", i.e. it is rejected outright
    assert prog["status"] == "insufficient_signal"
    assert prog["rul_days"] is None


def test_too_few_samples_rejects(w):
    """Before MIN_SAMPLES observations the tier declines to estimate a rate."""
    _drive(w, "sensor.pressure", 60.0, -0.2, MIN_SAMPLES - 5)
    prog = w.prognostics.latest[("house_a", "sensor.pressure")]
    assert prog["status"] == "insufficient_signal"


# ------------------------------------------------------------------ correct prognosis

def test_declining_pressure_yields_calibrated_rul(w):
    """A steady decline toward the low-pressure threshold produces an RUL whose interval
    contains the true remaining time, and a matching trend estimate."""
    # threshold is 40; start 60, fall 0.15/day
    final = _drive(w, "sensor.pressure", 60.0, -0.15, 80)
    prog = w.prognostics.latest[("house_a", "sensor.pressure")]
    assert prog["status"] == "degrading"
    true_rul = (final - 40.0) / 0.15
    lo, hi = prog["rul_interval_days"]
    assert lo <= true_rul <= hi, f"true RUL {true_rul:.0f} not in [{lo}, {hi}]"
    assert prog["trend_per_day"] == pytest.approx(0.15, abs=0.03)


def test_rising_signal_uses_correct_direction(w):
    """An 'up' asset (flow toward the abnormal-flow threshold) is handled with the right sign."""
    _drive(w, "sensor.flow_meter", 2.0, 0.25, 80)   # threshold 30, rising
    prog = w.prognostics.latest[("house_a", "sensor.flow_meter")]
    assert prog["status"] in {"degrading", "healthy_trend"}
    assert prog["rul_days"] is not None and prog["rul_days"] > 0


def test_publishes_prognosis_event(w):
    """Entering the alert horizon publishes exactly one advisory `prognosis` event."""
    seen = []
    w.bus.subscribe(lambda ev: seen.append(ev) if ev.type == "prognosis" else None)
    _drive(w, "sensor.pressure", 60.0, -0.2, 80)
    assert len(seen) == 1
    assert seen[0].data["status"] == "degrading"
    assert seen[0].entity_id == "house_a.sensor.pressure"


def test_report_shape(w):
    """The technician handoff carries RUL, interval, action deadline, degradation index."""
    _drive(w, "sensor.pressure", 60.0, -0.2, 80)
    rep = w.prognostics.report("house_a", "sensor.pressure")
    for key in ("asset_id", "status", "remaining_useful_life_days",
                "rul_80pct_interval_days", "action_deadline_days", "degradation_index_0to1"):
        assert key in rep
    assert rep["asset_id"] == "house_a.sensor.pressure"
    assert 0.0 <= rep["degradation_index_0to1"] <= 1.0
    assert rep["report_type"] == "pre-arrival equipment diagnostic"


def test_report_none_before_observation(w):
    """No report for an asset the tier has never seen."""
    assert w.prognostics.report("house_a", "sensor.does_not_exist") is None


# ------------------------------------------------------------------ never actuates

def test_prognostics_never_actuates(w):
    """The tier's only effects are an advisory event + its own estimate memory. Driving a
    degradation to the point of a prognosis moves no device state the tier didn't read."""
    before = {eid: e.state for h in w.houses.values() for eid, e in h.entities.items()}
    _drive(w, "sensor.pressure", 60.0, -0.2, 80)
    after = {eid: e.state for h in w.houses.values() for eid, e in h.entities.items()}
    changed = {k for k in before if before[k] != after[k]}
    # only the pressure sensor we drove ourselves changed; the tier actuated nothing
    assert changed <= {"house_a.sensor.pressure"}
    # and the water main was never touched despite a "degrading" water-pressure prognosis
    assert after["house_a.water.main_valve"] == "open"
