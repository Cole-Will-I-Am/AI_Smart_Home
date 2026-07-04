"""Deterministic anomaly baselines — the vigilance tier below the AI.

Learns a robust per-entity, per-hour-of-week baseline (median / MAD) from numeric
telemetry on the event bus and publishes typed `anomaly` events when an observation
deviates. This upgrades the house from *reactive* (fixed thresholds) to *vigilant*
(deviation from its own learned normal): a pipe weeping 3 L/min at 3 a.m., a furnace
whose duty cycle drifts, a circuit drawing power in an empty house — none of which
crosses a hard threshold — become visible.

Three properties are non-negotiable and mirrored in tests:

* **Advisory only.** An anomaly NEVER actuates anything by itself — it notifies (via the
  automations tier) and enriches what the AI and operators can observe (L0). Actuation
  still requires the independent physical signals the automations already demand
  (e.g. the two-signal leak rule). Statistics are evidence, not authority.
* **Deterministic.** Bounded sample windows, no wall clock (slots derive from the engine
  tick), no randomness: the same telemetry stream always yields the same anomalies.
* **Spike-proof scoring.** A sample is scored against the baseline *before* it is
  absorbed into it, so an outlier cannot dilute its own detection.

Stdlib-only, like the rest of the core.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .events import Event

SLOTS = 168                      # hour-of-week buckets: weekday rhythm != weekend rhythm
_MAD_TO_SIGMA = 1.4826           # consistency constant: MAD -> sigma under normality
_EPS = 1e-9


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def robust_z(value: float, samples: list[float]) -> tuple[float, float]:
    """(z, expected): deviation of `value` from the median in robust-sigma units.

    Median/MAD instead of mean/stddev so past anomalies in the window cannot drag the
    baseline toward themselves (breakdown point 50% vs 0%).
    """
    med = _median(samples)
    mad = _median([abs(x - med) for x in samples])
    sigma = _MAD_TO_SIGMA * mad
    return abs(value - med) / (sigma + _EPS), med


def _slope(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(xs) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    if denom <= _EPS:
        return 0.0
    return sum((i - mean_x) * (x - mean_y) for i, x in enumerate(xs)) / denom


@dataclass
class Anomaly:
    house_id: str
    entity_id: str
    value: float
    expected: float
    z: float
    n: int          # samples the verdict rests on
    slot: int       # hour-of-week bucket


class BaselineModel:
    """Per-(house, entity, slot) robust baselines over a bounded window.

    `min_samples` gates scoring (no verdicts from thin evidence); `abs_floor` is a
    per-entity dead-band so a near-zero MAD (very steady signal) cannot inflate trivial
    jitter into a statistical event: deviations must be material in *physical* units
    AND extreme in *statistical* units.
    """

    def __init__(self, window: int = 96, min_samples: int = 24,
                 z_threshold: float = 4.0, abs_floor: float | None = None,
                 floors: dict[str, float] | None = None) -> None:
        self.window = window
        self.min_samples = min_samples
        self.z_threshold = z_threshold
        self.default_floor = 0.0 if abs_floor is None else abs_floor
        self.floors = dict(floors or {})
        self._buckets: dict[tuple[str, str, int], deque[float]] = {}

    # -- learning / scoring ----------------------------------------------------------
    def observe(self, house_id: str, entity_id: str, value: float, slot: int) -> Anomaly | None:
        """Score `value` against the learned baseline, then absorb it. Returns an
        Anomaly when the deviation is both statistically extreme and physically material."""
        slot = int(slot) % SLOTS
        key = (house_id, entity_id, slot)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = deque(maxlen=self.window)
        verdict: Anomaly | None = None
        if len(bucket) >= self.min_samples:
            z, expected = robust_z(float(value), list(bucket))
            floor = self.floors.get(entity_id, self.default_floor)
            if z >= self.z_threshold and abs(float(value) - expected) >= floor:
                verdict = Anomaly(house_id, entity_id, float(value), round(expected, 4),
                                  round(z, 2), len(bucket), slot)
        # Absorb AFTER scoring — and never absorb a flagged outlier, so a sustained
        # attack/failure cannot teach the model that broken is normal.
        if verdict is None:
            bucket.append(float(value))
        return verdict

    def trend(self, house_id: str, entity_id: str, slot: int | None = None) -> dict:
        """Return a JSON-safe trend summary from learned buckets.

        If a slot is supplied and trained, use that bucket; otherwise aggregate all
        buckets for the entity. The result is pure read state for L0 tooling.
        """
        samples: list[float] = []
        if slot is not None:
            bucket = self._buckets.get((house_id, entity_id, int(slot) % SLOTS))
            if bucket is not None:
                samples = list(bucket)
        if not samples:
            for (h, e, _s), bucket in self._buckets.items():
                if h == house_id and e == entity_id:
                    samples.extend(bucket)
        if not samples:
            return {"samples": 0, "baseline": None, "slope": None}
        return {
            "samples": len(samples),
            "baseline": round(_median(samples), 4),
            "slope": round(_slope(samples), 6),
        }

    # -- persistence (JSON-safe) -----------------------------------------------------
    def to_dict(self) -> dict:
        return {f"{h}|{e}|{s}": list(v) for (h, e, s), v in self._buckets.items()}

    def load_dict(self, d: dict) -> None:
        for k, xs in d.items():
            h, e, s = k.rsplit("|", 2)
            self._buckets[(h, e, int(s))] = deque([float(x) for x in xs], maxlen=self.window)


class AnomalyMonitor:
    """Bus-attached vigilance: numeric telemetry in, `anomaly` events out.

    Watches the telemetry-bearing event types and their numeric payload keys; publishes
    Event("anomaly", ...) which the automations tier answers with a notification —
    and nothing else."""

    TELEMETRY: dict[str, str] = {"power_draw": "watts", "telemetry": "value", "temp": "temp"}

    def __init__(self, world, model: BaselineModel | None = None, ticks_per_hour: int = 1) -> None:
        self.world = world
        self.model = model or BaselineModel(floors={"flow": 5.0})
        self.ticks_per_hour = max(1, int(ticks_per_hour))

    def attach(self) -> "AnomalyMonitor":
        self.world.bus.subscribe(self._on_event)
        return self

    def _slot(self, tick: int) -> int:
        return (tick // self.ticks_per_hour) % SLOTS

    def _on_event(self, ev: Event) -> None:
        key = self.TELEMETRY.get(ev.type)
        if key is None or ev.entity_id is None:
            return
        raw = ev.data.get(key)
        if not isinstance(raw, (int, float)):
            return
        a = self.model.observe(ev.house_id, ev.entity_id, float(raw), self._slot(ev.tick))
        if a is not None:
            self.world.bus.publish(Event("anomaly", a.house_id, a.entity_id,
                                         {"value": a.value, "expected": a.expected,
                                          "z": a.z, "n": a.n, "metric": key},
                                         ev.tick))
