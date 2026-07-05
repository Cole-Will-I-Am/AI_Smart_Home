"""Predictive tier — a counterfactual gate over a physics-lite forward model.

The twin (twin.py) is described as "simulate before actuation" but is in fact a *static*
risk snapshot: it never rolls the estate forward. This tier adds the missing verb. Given a
proposed action it simulates the estate N steps ahead under a small, transparent physics
model (thermal drift, hydraulic pressure, battery state-of-charge under a shed, egress
reachability) and checks the predicted trajectory against a handful of end-state invariants:
no room driven below the freeze threshold, battery reserve never crossed mid-shed, no egress
path locked while a fire is inferred. It turns "verified after read-back" into "checked
before actuation."

DESIGN STANCE — advisory, deliberately not yet a gate. A forward model that has not been
validated against the real house can fail *confidently*: it is the one addition capable of
actively making a wrong call (a false ALLOW) rather than merely missing one. So this tier
ships in shadow: `assess()` returns an ALLOW/BLOCK verdict and publishes a `counterfactual`
advisory, but nothing in the actuation path consults it. Promotion to a hard pre-actuation
gate is earned, not assumed — earned by `validate()`, a harness that replays each prediction
against the state actually observed afterward and reports the model's error. Only once that
error is demonstrably small on a given estate should an operator flip it to enforcing.

Everything here is deterministic and side-effect-free except the single advisory publish, so
a prediction + the invariant it checked can be hash-chained beside the action it concerns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .events import Event

# --- physics-lite constants (declared, inspectable; tune per estate) -----------------
FREEZE_THRESHOLD_F = 36.0     # a predicted room temp below this is a frozen-pipe risk
HEAT_RATE_F = 2.0             # deg/step a called-for zone gains
DRIFT_RATE_F = 1.0           # deg/step a zone loses toward a cold outside when heat is cut
OUTSIDE_F = 20.0             # assumed cold-snap outside temp for drift
BATTERY_RESERVE = 20.0       # % SoC that must never be crossed during a load-shed
SHED_DRAW_PER_STEP = 3.0     # % SoC/step drawn while on battery under load
PRESSURE_NOMINAL = 60.0      # static pressure when the main is open
HORIZON = 6                  # default look-ahead steps


@dataclass(frozen=True)
class Prediction:
    allow: bool
    horizon: int
    violated: tuple[str, ...]           # invariant ids the predicted trajectory breaks
    trajectory: dict                    # per-quantity list of predicted values (for audit/plots)
    note: str = ""


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class ForwardModel:
    """A tiny, explicit estate dynamics model. Reads current state, returns per-step
    trajectories for the quantities the invariants care about. No I/O, no actuation."""

    def __init__(self, world) -> None:
        self.world = world

    def _get(self, house_id: str, rel: str, default=None):
        return self.world.state.get_state(f"{house_id}.{rel}")

    def rollout(self, house_id: str, action: dict, horizon: int) -> dict:
        st = self.world.state
        subsystem = action.get("subsystem")
        act = action.get("action")
        args = action.get("args", {})

        # seed quantities from live state
        temp = _num(self._get(house_id, "sensor.temp_basement"), 55.0)
        soc = _num((st.entity(f"{house_id}.battery.main").attributes.get("soc")
                    if st.entity(f"{house_id}.battery.main") else None), 100.0)
        pressure = PRESSURE_NOMINAL if self._get(house_id, "water.main_valve") == "open" else 0.0
        egress_locked = self._get(house_id, "lock.egress_side") != "unlocked"

        # how the proposed action perturbs the dynamics
        heating = (subsystem == "climate" and act == "set_temperature"
                   and _num(args.get("temperature"), 0) >= 70)
        cutting_heat = subsystem == "hvac" and act == "emergency_shutoff"
        shedding = subsystem == "power" and act == "load_shed"
        closing_main = subsystem == "water" and act == "shutoff_main"
        locking_egress = subsystem == "lock" and act == "lock" and "egress" in str(action.get("target", ""))

        temps, socs, pressures = [], [], []
        for _ in range(horizon):
            if heating:
                temp += HEAT_RATE_F
            elif cutting_heat:
                temp -= DRIFT_RATE_F * (1 if temp > OUTSIDE_F else 0)
            if shedding:
                soc = max(0.0, soc - SHED_DRAW_PER_STEP)
            if closing_main:
                pressure = 0.0
            temps.append(round(temp, 2))
            socs.append(round(soc, 2))
            pressures.append(round(pressure, 2))

        return {"temp_f": temps, "battery_soc": socs, "pressure": pressures,
                "egress_locked_final": bool(egress_locked or locking_egress)}


@dataclass
class CounterfactualGate:
    """Advisory forward-simulation check. `assess()` never actuates and is not consulted by
    the router; it only advises. `enforcing` exists so an operator can *later* opt in per
    estate once `validate()` shows the model is trustworthy — it defaults OFF."""
    world: object | None = None
    horizon: int = HORIZON
    enforcing: bool = False
    fire_inferred: Callable[[str], bool] | None = None
    predictions: list[dict] = field(default_factory=list)

    def attach(self, world) -> "CounterfactualGate":
        self.world = world
        self.model = ForwardModel(world)
        return self

    def _invariants(self, house_id: str, traj: dict) -> list[str]:
        violated = []
        if traj["temp_f"] and min(traj["temp_f"]) < FREEZE_THRESHOLD_F:
            violated.append("no_room_below_freeze")
        if traj["battery_soc"] and min(traj["battery_soc"]) < BATTERY_RESERVE:
            violated.append("battery_reserve_preserved")
        fire = self.fire_inferred(house_id) if self.fire_inferred else False
        if fire and traj["egress_locked_final"]:
            violated.append("egress_open_while_fire")
        return violated

    def assess(self, house_id: str, action: dict) -> Prediction:
        """Simulate `action` forward and check end-state invariants. Advisory: returns a
        verdict and publishes a `counterfactual` event; actuation does not depend on it."""
        traj = self.model.rollout(house_id, action, self.horizon)
        violated = tuple(self._invariants(house_id, traj))
        pred = Prediction(allow=not violated, horizon=self.horizon, violated=violated,
                          trajectory=traj,
                          note="advisory" if not self.enforcing else "enforcing")
        rec = {"house_id": house_id, "action": {k: action.get(k) for k in ("subsystem", "target", "action")},
               "allow": pred.allow, "violated": list(violated), "enforcing": self.enforcing}
        self.predictions.append(rec)
        if self.world is not None:
            self.world.bus.publish(Event("counterfactual", house_id,
                                         action.get("target"), rec, self.world.engine.tick))
        return pred

    # --- promotion harness: is the model good enough to ever gate? -------------------
    def validate(self, samples: list[dict]) -> dict:
        """Replay predictions against observed post-states to quantify model error before
        anyone trusts this as a gate. Each sample: {predicted: float, observed: float}.
        Returns mean/max absolute error and a naive pass flag. This is how an operator earns
        the right to set `enforcing=True` — the answer to 'twin divergence gives false
        confidence' is to measure the divergence, not assume it away."""
        if not samples:
            return {"n": 0, "mae": None, "max_ae": None, "trustworthy": False}
        errs = [abs(_num(s["predicted"], 0.0) - _num(s["observed"], 0.0)) for s in samples]
        mae = sum(errs) / len(errs)
        return {"n": len(samples), "mae": round(mae, 3), "max_ae": round(max(errs), 3),
                "trustworthy": max(errs) <= 2.0 and mae <= 1.0}
