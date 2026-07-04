"""Day-ahead energy planner — deterministic economics below the AI.

Given a tariff, a solar forecast, a load forecast, battery limits, and an optional EV
requirement, computes an hourly schedule (battery charge/discharge, EV charging window)
that reduces grid cost, alongside the do-nothing baseline it must beat. The algorithm is
a greedy price-quantile heuristic — auditable line by line, deterministic, stdlib-only —
deliberately NOT an opaque optimizer: in a house, an explainable plan you can veto beats
an optimal plan you can't inspect.

Authority is unchanged by this module. The planner's output is *advice with a price
tag*: `DayPlan.intents()` renders the schedule as proposed `battery set_mode` /
`evcharger set_limit` intents — both L3 — that face the same permission engine and hardware
approval as any operator — with human confirmation required when the proposer is an AI. The planner cannot actuate anything.

Guaranteed properties (each has a regression test):
* SOC stays within [0, capacity] and rates within limits at every hour.
* The EV receives its requested energy, only inside its window.
* **Never-worse:** if the heuristic fails to beat the do-nothing baseline for a given
  day shape, the planner returns the baseline (battery idle) — the plan's cost is
  <= the baseline's by construction, or the plan IS the baseline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .permissions import Intent

HOURS = 24


@dataclass
class Battery:
    capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    soc_kwh: float = 0.0
    efficiency: float = 0.90     # round-trip, applied on charge


@dataclass
class DayInputs:
    import_price: list[float]                 # $/kWh, 24 entries
    solar_kw: list[float]                     # forecast production
    load_kw: list[float]                      # forecast base load
    export_price: list[float] | None = None   # $/kWh credit (default: no credit)
    ev_kwh: float = 0.0                       # energy the EV must receive today
    ev_window: tuple[int, int] = (22, 6)      # [start, end) hours, may wrap midnight
    ev_max_kw: float = 7.2

    def window_hours(self) -> list[int]:
        a, b = self.ev_window
        return list(range(a, b)) if a < b else list(range(a, HOURS)) + list(range(0, b))


@dataclass
class HourPlan:
    hour: int
    price: float
    load_kw: float
    solar_kw: float
    ev_kw: float
    battery_kw: float     # +charge / -discharge (at the meter)
    grid_kw: float        # +import / -export
    soc_kwh: float        # end of hour


@dataclass
class DayPlan:
    hours: list[HourPlan]
    cost: float
    baseline_cost: float
    used_baseline: bool = False
    savings: float = field(init=False)

    def __post_init__(self) -> None:
        self.savings = round(self.baseline_cost - self.cost, 4)

    # -- schedule -> proposed intents (still face the L3 gate) ------------------------
    def intents(self, house_id: str) -> list[tuple[int, Intent]]:
        """Battery regime changes and EV limit changes as (hour, Intent) pairs.
        Consecutive identical regimes are compressed; every intent is L3 and must pass
        the permission engine's hardware-approval + confirm dance to execute."""
        out: list[tuple[int, Intent]] = []
        prev_mode = None
        prev_ev = 0.0
        for hp in self.hours:
            mode = "charge" if hp.battery_kw > 1e-9 else ("discharge" if hp.battery_kw < -1e-9 else "auto")
            if mode != prev_mode:
                out.append((hp.hour, Intent(house_id, "battery", "main", "set_mode",
                                            {"mode": mode, "kw": round(abs(hp.battery_kw), 2)})))
                prev_mode = mode
            if (hp.ev_kw > 1e-9) != (prev_ev > 1e-9):
                amps = int(round(hp.ev_kw * 1000 / 240)) if hp.ev_kw > 1e-9 else 0
                out.append((hp.hour, Intent(house_id, "evcharger", "main", "set_limit", {"amps": amps})))
                prev_ev = hp.ev_kw
        return out


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(q * (len(s) - 1))))
    return s[i]


def _simulate(inputs: DayInputs, battery: Battery | None,
              ev_kw: list[float], charge_h: set[int], discharge_h: set[int]) -> tuple[list[HourPlan], float]:
    exp = inputs.export_price or [0.0] * HOURS
    soc = battery.soc_kwh if battery else 0.0
    plan: list[HourPlan] = []
    cost = 0.0
    for h in range(HOURS):
        load, sun, price = inputs.load_kw[h], inputs.solar_kw[h], inputs.import_price[h]
        net = load + ev_kw[h] - sun            # + means the house needs energy
        batt = 0.0
        if battery:
            if net < 0:                        # solar surplus -> battery first, always
                room = (battery.capacity_kwh - soc) / battery.efficiency
                batt = min(-net, battery.max_charge_kw, room)
                soc += batt * battery.efficiency
                net += batt
            elif h in charge_h:                # cheap hour -> buy energy into the battery
                room = (battery.capacity_kwh - soc) / battery.efficiency
                batt = min(battery.max_charge_kw, room)
                soc += batt * battery.efficiency
                net += batt
            elif h in discharge_h and net > 0:  # dear hour -> serve load from the battery
                give = min(battery.max_discharge_kw, soc, net)
                soc -= give
                net -= give
                batt = -give
        grid = net
        cost += grid * price if grid >= 0 else grid * exp[h]   # exports earn export_price
        plan.append(HourPlan(h, price, load, sun, ev_kw[h], round(batt, 4),
                             round(grid, 4), round(soc, 4)))
    return plan, round(cost, 4)


def plan_day(inputs: DayInputs, battery: Battery | None = None) -> DayPlan:
    """Deterministic greedy plan: EV into the cheapest window hours; battery buys in the
    bottom price quartile, sells to the house in the top quartile; solar surplus is
    always banked. Falls back to the do-nothing baseline if the heuristic doesn't pay."""
    if len(inputs.import_price) != HOURS or len(inputs.solar_kw) != HOURS or len(inputs.load_kw) != HOURS:
        raise ValueError("import_price / solar_kw / load_kw must each have 24 entries")
    # 1. EV: cheapest feasible hours inside the window (deterministic tie-break by hour).
    ev_kw = [0.0] * HOURS
    remaining = max(0.0, inputs.ev_kwh)
    for h in sorted(inputs.window_hours(), key=lambda h: (inputs.import_price[h], h)):
        if remaining <= 1e-9:
            break
        take = min(inputs.ev_max_kw, remaining)
        ev_kw[h] = take
        remaining -= take
    if remaining > 1e-9:
        raise ValueError(f"EV window cannot deliver {inputs.ev_kwh} kWh "
                         f"(short {round(remaining, 2)} kWh at {inputs.ev_max_kw} kW)")
    # 2. Battery regimes from price quartiles.
    cheap = _quantile(inputs.import_price, 0.25)
    dear = _quantile(inputs.import_price, 0.75)
    charge_h = {h for h in range(HOURS) if inputs.import_price[h] <= cheap}
    discharge_h = {h for h in range(HOURS) if inputs.import_price[h] >= dear}
    # 3. Simulate plan and baseline (same EV need, battery idle, no solar banking).
    plan_hours, plan_cost = _simulate(inputs, battery, ev_kw, charge_h, discharge_h)
    base_hours, base_cost = _simulate(inputs, None, ev_kw, set(), set())
    # 4. Never-worse guarantee.
    honest_plan_cost = plan_cost
    terminal_soc = plan_hours[-1].soc_kwh if battery and plan_hours else 0.0
    if battery is not None and terminal_soc + 1e-9 < battery.soc_kwh:
        replacement = (battery.soc_kwh - terminal_soc) * max(inputs.import_price)
        honest_plan_cost = round(plan_cost + replacement, 4)
    if honest_plan_cost > base_cost:
        return DayPlan(base_hours, base_cost, base_cost, used_baseline=True)
    return DayPlan(plan_hours, honest_plan_cost, base_cost)


# -- a plausible deterministic day for demos / CLI ------------------------------------
def example_day() -> tuple[DayInputs, Battery]:
    """Synthetic but physically sensible: TOU tariff with an evening peak, a midday
    solar bell, an evening load bump, and an overnight EV charge requirement."""
    price = ([0.09] * 6 + [0.16] * 10 + [0.38] * 5 + [0.16] * 3)
    solar = [0, 0, 0, 0, 0, 0, 0.2, 0.9, 2.2, 3.6, 4.6, 5.1,
             5.1, 4.6, 3.6, 2.2, 0.9, 0.2, 0, 0, 0, 0, 0, 0]
    load = [0.8, 0.7, 0.7, 0.7, 0.8, 1.0, 1.6, 2.0, 1.8, 1.5, 1.4, 1.5,
            1.6, 1.5, 1.4, 1.6, 2.2, 3.2, 4.0, 3.8, 3.0, 2.2, 1.4, 1.0]
    return (DayInputs(import_price=price, solar_kw=solar, load_kw=load,
                      ev_kwh=10.0, ev_window=(22, 6), ev_max_kw=7.2),
            Battery(capacity_kwh=13.5, max_charge_kw=5.0, max_discharge_kw=5.0,
                    soc_kwh=2.0, efficiency=0.90))
