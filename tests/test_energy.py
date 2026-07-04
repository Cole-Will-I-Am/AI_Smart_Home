"""Economics tier: physical bounds, EV delivery, never-worse guarantee, L3 gating."""
import pytest

from homeops.energy import HOURS, Battery, DayInputs, example_day, plan_day
from homeops.permissions import Operator


def owner():
    return Operator("owner", "house_a")


def ai():
    return Operator("ai", "house_a")


def test_example_day_saves_money():
    inputs, batt = example_day()
    plan = plan_day(inputs, batt)
    assert plan.savings > 0 and not plan.used_baseline
    assert plan.cost < plan.baseline_cost


def test_physical_bounds_every_hour():
    inputs, batt = example_day()
    plan = plan_day(inputs, batt)
    for hp in plan.hours:
        assert -1e-6 <= hp.soc_kwh <= batt.capacity_kwh + 1e-6
        assert hp.battery_kw <= batt.max_charge_kw + 1e-6
        assert -hp.battery_kw <= batt.max_discharge_kw + 1e-6
        assert hp.ev_kw <= inputs.ev_max_kw + 1e-6


def test_ev_receives_its_energy_only_inside_window():
    inputs, batt = example_day()
    plan = plan_day(inputs, batt)
    delivered = sum(hp.ev_kw for hp in plan.hours)
    assert abs(delivered - inputs.ev_kwh) < 1e-6
    window = set(inputs.window_hours())
    assert all(hp.ev_kw == 0 for hp in plan.hours if hp.hour not in window)


def test_infeasible_ev_window_fails_loudly():
    inputs, _ = example_day()
    inputs.ev_kwh = 200.0                                    # cannot fit at 7.2 kW
    with pytest.raises(ValueError):
        plan_day(inputs)


def test_never_worse_flat_prices_fall_back_to_baseline():
    # no arbitrage exists -> the plan must equal the do-nothing baseline, not lose money
    inputs = DayInputs(import_price=[0.20] * HOURS, solar_kw=[0.0] * HOURS,
                       load_kw=[1.0] * HOURS)
    plan = plan_day(inputs, Battery(13.5, 5.0, 5.0, soc_kwh=0.0))
    assert plan.savings == 0.0
    assert plan.cost <= plan.baseline_cost


def test_never_worse_does_not_report_savings_from_depleting_battery_soc():
    inputs = DayInputs(import_price=[1.00] + [0.10] * (HOURS - 1),
                       solar_kw=[0.0] * HOURS, load_kw=[1.0] * HOURS)
    plan = plan_day(inputs, Battery(capacity_kwh=5.0, max_charge_kw=0.0,
                                    max_discharge_kw=5.0, soc_kwh=5.0))
    assert plan.savings == 0.0
    assert plan.cost == plan.baseline_cost


def test_plan_intents_face_the_L3_gate(bare):
    """The planner proposes; the engine disposes. Battery set_mode is L3
    hardware-gated and structurally confirmed: an AI operator proposing a plan step gets
    confirm_required with NO token, and an owner must complete the same token dance. The
    planner holds no authority of its own."""
    inputs, batt = example_day()
    plan = plan_day(inputs, batt)
    pairs = plan.intents("house_a")
    battery_intents = [i for _, i in pairs if i.subsystem == "battery"]
    assert battery_intents, "plan must contain battery regime changes"
    intent = battery_intents[0]
    r_ai = bare.router.execute(intent, ai())
    assert r_ai.status == "confirm_required" and r_ai.confirm_token is None
    r_owner = bare.router.execute(intent, owner())
    assert r_owner.status == "confirm_required" and r_owner.confirm_token
    intent.confirm_token = r_owner.confirm_token
    assert bare.router.execute(intent, owner()).status == "executed"


def test_solar_surplus_is_banked_before_export():
    inputs, batt = example_day()
    plan = plan_day(inputs, batt)
    # at the solar peak the battery is charging or full — surplus never bypasses it
    noon = plan.hours[12]
    assert noon.battery_kw >= 0
    if noon.soc_kwh < batt.capacity_kwh - 1e-6:
        assert noon.battery_kw > 0 or noon.solar_kw <= noon.load_kw + noon.ev_kw
