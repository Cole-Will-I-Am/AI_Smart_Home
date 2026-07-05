"""Prognostics tier — remaining-useful-life estimation over the home's own telemetry.

The vigilance tier (baseline.py) answers "is this reading abnormal *now*?". This tier answers
the orthogonal question "how long until this signal reaches a failure/actionable level?" — it
turns the sensor exhaust the home already collects for convenience into *equipment* health
information, and, when a trend is credible, produces a remaining-useful-life (RUL) estimate a
technician can act on before the failure manifests.

The mechanism is a small online Kalman filter per monitored signal, tracking [level, rate]
with a random-walk rate. Given a declared failure threshold and the direction the signal
moves toward it, the remaining distance divided by the credibly-estimated rate gives a point
RUL; the rate's own posterior uncertainty gives an analytic interval. This is deliberately a
lightweight, DETERMINISTIC estimator kept in the stdlib-only core — the full inverse-Gaussian
first-passage treatment, fleet calibration (PIT coverage), model-misspecification stress, and
the load-confound / observability boundary live in the offline study (phm_home.py). What the
runtime tier keeps is the part that must be honest online:

  - a REJECT-OPTION: if the wear rate is not credibly toward the threshold (signal-to-noise on
    the rate below a floor) or too few samples have accrued, the tier says "insufficient
    signal" rather than emit a confident, wrong number — the honest answer for a flat or
    unobservable signal.
  - failing safe: like vigilance and inference, it is a pure reducer over live state plus its
    own filter memory. It publishes an advisory `prognosis` event and NEVER actuates. A wrong
    prognosis costs a needless inspection, never an unsafe action.

Thresholds and directions are declared constants an estate engineer can read and argue with —
the same design stance the rest of the stack takes toward its parameters.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .events import Event

# --- tunables (declared, inspectable) ------------------------------------------------
SNR_MIN = 3.0                 # rate must exceed this many posterior-std-devs to be "credible" (noise stays below ~1.5; real trends reach ~48, so 3.0 rejects noise with wide margin)
MIN_SAMPLES = 30             # don't estimate a rate from too few observations
Z80 = 1.2816                 # ~80% one-sided normal quantile for the RUL interval
ALERT_HORIZON = 60.0         # publish a prognosis when median RUL falls within this many days
HORIZON_CAP = 5 * 365.0      # clamp RUL estimates to a sane ceiling
DT = 1.0                     # one evaluation == one day (matches World.tick cadence)

# Kalman process/observation noise (per monitored signal). Small rate-walk => steady estimate.
Q_LEVEL = 1e-4
Q_RATE = 3e-7
R_MEAS = 4e-2


@dataclass(frozen=True)
class MonitoredAsset:
    """One signal to prognose: a house-relative entity id, the failure threshold on its value,
    and the direction the value moves as it approaches failure ('up' or 'down')."""
    suffix: str                  # e.g. "sensor.pressure" (fully qualified as f"{house}.{suffix}")
    label: str
    threshold: float
    direction: str               # "up" (rising to threshold) | "down" (falling to threshold)
    unit: str = ""


# Default watch-list: real canonical numeric sensors with physically meaningful action levels.
# On a healthy (flat) house every one of these correctly rejects — the tier stays silent until
# a genuine trend appears. Equipment-wear (the motivating case) is demonstrated in the offline
# study; here the tier watches whatever numeric telemetry the reference model actually exposes.
DEFAULT_ASSETS: tuple[MonitoredAsset, ...] = (
    MonitoredAsset("sensor.pressure", "water-system pressure", 40.0, "down", "psi"),
    MonitoredAsset("sensor.flow_meter", "water flow", 30.0, "up", "L/min"),
    MonitoredAsset("sensor.co2", "indoor CO2", 1200.0, "up", "ppm"),
    MonitoredAsset("sensor.temp_basement", "basement temperature", 38.0, "down", "degF"),
)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class _Track:
    """Per-(house, asset) filter state: level x, rate v, and the 2x2 covariance (symmetric)."""
    x: float
    v: float = 0.0
    p00: float = 1e-3
    p01: float = 0.0
    p11: float = 1e-5
    n: int = 0
    baseline: float | None = None

    def step(self, z: float) -> None:
        dt = DT
        # --- predict (F = [[1,dt],[0,1]]) ---
        self.x = self.x + dt * self.v
        p00 = self.p00 + dt * (2 * self.p01) + dt * dt * self.p11 + Q_LEVEL
        p01 = self.p01 + dt * self.p11
        p11 = self.p11 + Q_RATE
        # --- update against scalar level measurement (H = [1,0]) ---
        s = p00 + R_MEAS
        k0 = p00 / s
        k1 = p01 / s
        innov = z - self.x
        self.x += k0 * innov
        self.v += k1 * innov
        self.p00 = (1 - k0) * p00
        self.p01 = (1 - k0) * p01
        self.p11 = p11 - k1 * p01
        self.n += 1


def _distance_and_rate(track: _Track, asset: MonitoredAsset) -> tuple[float, float, float]:
    """Remaining distance to threshold (>0 while healthy), rate TOWARD failure (>0 approaching),
    and the rate's posterior std-dev."""
    if asset.direction == "up":
        dist = asset.threshold - track.x
        rate = track.v
    else:
        dist = track.x - asset.threshold
        rate = -track.v
    sd_rate = math.sqrt(max(track.p11, 1e-12))
    return dist, rate, sd_rate


@dataclass
class PrognosticsMonitor:
    """Bus-attached, per-tick online prognostics. Its only outward effects are an advisory
    `prognosis` event and a queryable latest estimate per asset. It never actuates."""
    assets: tuple[MonitoredAsset, ...] = DEFAULT_ASSETS
    world: object | None = None
    tracks: dict[tuple[str, str], _Track] = field(default_factory=dict)
    latest: dict[tuple[str, str], dict] = field(default_factory=dict)
    _alerted: set[tuple[str, str]] = field(default_factory=set)

    def attach(self, world) -> "PrognosticsMonitor":
        self.world = world
        return self

    def _houses(self):
        return list(self.world.houses.keys())

    def evaluate(self) -> list[dict]:
        """Step every asset's filter from current state and refresh its prognosis. Called each
        World.tick. Publishes an advisory when an asset first enters the alert horizon."""
        if self.world is None:
            return []
        fired: list[dict] = []
        tick = self.world.engine.tick
        for house_id in self._houses():
            for asset in self.assets:
                z = _num(self.world.state.get_state(f"{house_id}.{asset.suffix}"))
                if z is None:
                    continue                              # non-numeric telemetry: cannot prognose
                key = (house_id, asset.suffix)
                tr = self.tracks.get(key)
                if tr is None:
                    tr = self.tracks[key] = _Track(x=z, baseline=z)
                    continue
                tr.step(z)
                prog = self._prognose(house_id, asset, tr, tick)
                self.latest[key] = prog
                if prog["status"] == "degrading" and key not in self._alerted:
                    self._alerted.add(key)
                    fired.append(prog)
                    self.world.bus.publish(Event("prognosis", house_id,
                                                 f"{house_id}.{asset.suffix}", prog, tick))
                elif prog["status"] != "degrading":
                    self._alerted.discard(key)
        return fired

    def _prognose(self, house_id: str, asset: MonitoredAsset, tr: _Track, tick: int) -> dict:
        dist, rate, sd_rate = _distance_and_rate(tr, asset)
        base = {"asset": asset.suffix, "label": asset.label, "house_id": house_id,
                "unit": asset.unit, "tick": tick, "samples": tr.n, "level": round(tr.x, 3)}
        if dist <= 0:
            return {**base, "status": "at_or_past_threshold", "rul_days": 0.0,
                    "rul_interval_days": [0.0, 0.0]}
        if tr.n < MIN_SAMPLES or sd_rate <= 0 or rate / sd_rate < SNR_MIN:
            return {**base, "status": "insufficient_signal", "rul_days": None,
                    "rul_interval_days": None,
                    "reason": "no credible trend toward threshold (reject-option)"}
        rul = min(dist / rate, HORIZON_CAP)
        rate_hi = rate + Z80 * sd_rate
        rate_lo = rate - Z80 * sd_rate
        rul_lo = min(dist / rate_hi, HORIZON_CAP) if rate_hi > 0 else HORIZON_CAP
        rul_hi = min(dist / rate_lo, HORIZON_CAP) if rate_lo > 0 else HORIZON_CAP
        status = "degrading" if rul <= ALERT_HORIZON else "healthy_trend"
        return {**base, "status": status, "rul_days": round(rul, 1),
                "rul_interval_days": [round(min(rul_lo, rul_hi), 1), round(max(rul_lo, rul_hi), 1)],
                "trend_per_day": round(rate, 5)}

    # --- technician handoff ----------------------------------------------------------
    def report(self, house_id: str, suffix: str) -> dict | None:
        """The pre-arrival diagnostic for one asset: current prognosis plus a degradation index
        (0 = commissioning baseline, 1 = failure threshold) and the action deadline. None if the
        asset has not been observed."""
        key = (house_id, suffix)
        prog = self.latest.get(key)
        tr = self.tracks.get(key)
        if prog is None or tr is None:
            return None
        asset = next((a for a in self.assets if a.suffix == suffix), None)
        idx = None
        if asset is not None and tr.baseline is not None and asset.threshold != tr.baseline:
            span = asset.threshold - tr.baseline
            idx = max(0.0, min(1.0, (tr.x - tr.baseline) / span))
        deadline = (prog["rul_interval_days"][0]
                    if prog.get("rul_interval_days") else None)
        return {
            "report_type": "pre-arrival equipment diagnostic",
            "asset_id": f"{house_id}.{suffix}",
            "label": prog["label"],
            "status": prog["status"],
            "remaining_useful_life_days": prog.get("rul_days"),
            "rul_80pct_interval_days": prog.get("rul_interval_days"),
            "action_deadline_days": deadline,
            "degradation_index_0to1": None if idx is None else round(idx, 2),
            "trend_per_day": prog.get("trend_per_day"),
            "samples": prog["samples"],
            "caveats": [
                "Valid for wear-out modes with an observable trend; sudden failure is not predicted.",
                "RUL interval is model-based; act on the lower bound, not the median.",
                "Confidence depends on signal quality; a load-confounded proxy can over-call.",
            ],
        }
