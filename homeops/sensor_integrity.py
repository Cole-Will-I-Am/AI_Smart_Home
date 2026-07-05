"""Sensor-integrity tier — causal consistency over physically-coupled sensors.

The two-signal rule (automations.py) re-reads two *independent* channels before a
destructive actuation. Independence is necessary but not sufficient: an attacker who owns
the sensor plane can drive two independent channels to individually-plausible values that
are *jointly impossible* — a flow meter pinned high while pressure sits flat, motion in
every zone while whole-home power draws nothing. Independence says "two sources agreed";
it cannot say "the physics that couples those sources was respected."

This tier adds that second question. Physically-coupled sensors have an expected *joint*
relationship the real world cannot violate; a compromised sensor plane usually can. Each
`CouplingRule` encodes one such physical law as a cheap, inspectable predicate over live
state. A violated coupling debits the *trust* of the sensors it names; satisfied couplings
slowly repay it. The result is a per-entity trust score in [0,1].

The tier holds NO authority and actuates nothing — like vigilance and inference, it is a
pure reducer over live state plus a small amount of its own trust memory. Its single
outward effect is a predicate, `trusts(entity_id)`, that the two-signal gate consults: a
sensor below the trust floor may no longer *satisfy* a two-signal requirement. It can only
ever make the engine MORE reluctant to fire a destructive action, never less — so a bug
here fails safe (a spurious low score costs a confirmation, never an unsafe actuation).

Coupling weights and the physics thresholds are declared constants, not learned, so an
estate-security consultant can read, argue with, and tune them — the same design stance
twin.py takes toward its risk weights.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .events import Event

# --- tunables (declared, inspectable) ------------------------------------------------
TRUST_FLOOR = 0.5          # below this a sensor may not satisfy a two-signal requirement
DEBIT = 0.34               # trust lost per violated coupling that names the sensor
REPAY = 0.05               # trust regained per tick a naming coupling holds (slow)
MIN_TRUST, MAX_TRUST = 0.0, 1.0

# Physics thresholds, mirrored from the automations layer where they overlap.
ABNORMAL_FLOW = 30.0       # L/min — matches automations.ABNORMAL_FLOW
FLOW_ACTIVE = 5.0          # any real draw above trickle
PRESSURE_NOMINAL = 45.0    # static pressure floor; a real high-flow event pulls below this
CO2_STEP = 40              # ppm/occupant-tick a real occupied room accumulates
MOTION_POWER_FLOOR = 50.0  # W — a genuinely occupied, active house draws at least this


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CouplingRule:
    """One physical law coupling >=2 sensors, as a predicate over a state-reader.

    `holds(get)` returns True when the joint reading is physically consistent, False when
    it is impossible (=> debit), or None when the law does not apply this tick (=> abstain).
    `sensors` are the house-relative entity ids whose trust the rule adjudicates.
    """
    name: str
    sensors: tuple[str, ...]
    holds: Callable[[Callable[[str], object]], bool | None]
    because: str = ""


def _flow_pressure(get) -> bool | None:
    """A real high-flow event MUST pull static pressure down. High flow + flat pressure
    is the signature of a spoofed flow meter (or a stuck pressure sensor)."""
    flow = _num(get("sensor.flow_meter"))
    press = _num(get("sensor.pressure"))
    if flow is None or press is None:
        return None
    if flow < ABNORMAL_FLOW:
        return None                     # law only bites in the regime that triggers shutoff
    return press < PRESSURE_NOMINAL     # consistent iff pressure actually dropped


def _co2_occupancy(get) -> bool | None:
    """CO2 well above baseline with zero occupancy and no ventilation fault is not a thing
    people can produce; it points at a spoofed CO2 line driving a ventilation response."""
    co2 = _num(get("sensor.co2"))
    occ = _num(get("sensor.occupancy"))
    if co2 is None or occ is None:
        return None
    if co2 < 1200:                      # only adjudicate clearly-elevated CO2
        return None
    return occ >= 1                     # elevated CO2 is consistent only with someone present


def _motion_power(get) -> bool | None:
    """Whole-house motion while the panel reports ~zero draw is the classic replayed-PIR /
    frozen-camera signature: bodies moving but nothing electrical stirring."""
    motion = get("sensor.motion_front")
    watts = _num(get("power.panel#watts"))
    if watts is None or motion is None:
        return None
    if str(motion) != "detected":
        return None
    return watts >= MOTION_POWER_FLOOR


DEFAULT_COUPLINGS: tuple[CouplingRule, ...] = (
    CouplingRule("flow_implies_pressure_drop",
                 ("sensor.flow_meter", "sensor.pressure"), _flow_pressure,
                 "high flow must pull static pressure below nominal"),
    CouplingRule("co2_implies_occupancy",
                 ("sensor.co2", "sensor.occupancy"), _co2_occupancy,
                 "elevated CO2 implies a person is present"),
    CouplingRule("motion_implies_power",
                 ("sensor.motion_front", "power.panel#watts"), _motion_power,
                 "an occupied, active house draws measurable power"),
)


@dataclass
class SensorIntegrity:
    """Bus-attached causal-consistency reducer producing a per-entity trust map.

    Trust memory is the tier's ONLY state; it is bounded to [0,1] and rebuildable by
    replaying state. The tier publishes an advisory `sensor_integrity` event on a violation
    (for L0 awareness and the conformance monitor) and never touches the router.
    """
    couplings: tuple[CouplingRule, ...] = DEFAULT_COUPLINGS
    world: object | None = None
    trust: dict[str, float] = field(default_factory=dict)
    last_violations: dict[str, list[str]] = field(default_factory=dict)

    def attach(self, world) -> "SensorIntegrity":
        self.world = world
        world.bus.subscribe(self._on_event)
        return self

    # --- trust map -------------------------------------------------------------------
    def score(self, entity_id: str) -> float:
        return self.trust.get(entity_id, MAX_TRUST)

    def trusts(self, entity_id: str) -> bool:
        """The outward predicate the two-signal gate consults. Unknown => trusted (the tier
        only ever *lowers* confidence; absence of evidence is not evidence of tampering)."""
        return self.score(entity_id) >= TRUST_FLOOR

    def _fq(self, house_id: str, rel: str) -> str:
        return f"{house_id}.{rel}"

    def _adjust(self, eid: str, delta: float) -> None:
        self.trust[eid] = max(MIN_TRUST, min(MAX_TRUST, self.score(eid) + delta))

    def evaluate(self, house_id: str) -> list[str]:
        """Run every coupling for one house; debit violators, repay the consistent.
        Returns the list of violated coupling names (also cached for L0 queries)."""
        assert self.world is not None
        def get(rel):
            if "#" in rel:                         # "power.panel#watts" -> entity attribute
                base, attr = rel.split("#", 1)
                e = self.world.state.entity(self._fq(house_id, base))
                return None if e is None else e.attributes.get(attr)
            return self.world.state.get_state(self._fq(house_id, rel))
        violated: list[str] = []
        for rule in self.couplings:
            verdict = rule.holds(get)
            if verdict is None:
                continue                                  # law inapplicable this tick
            for rel in rule.sensors:
                eid = self._fq(house_id, rel)
                self._adjust(eid, REPAY if verdict else -DEBIT)
            if not verdict:
                violated.append(rule.name)
        self.last_violations[house_id] = violated
        return violated

    def _on_event(self, ev: Event) -> None:
        # Re-adjudicate on any sensor/state/power event; ignore our own + advisory chatter.
        if self.world is None or ev.type in {"sensor_integrity", "inference", "anomaly"}:
            return
        before = dict(self.trust)
        violated = self.evaluate(ev.house_id)
        if not violated:
            return
        newly_untrusted = [e for e, v in self.trust.items()
                           if v < TRUST_FLOOR and before.get(e, MAX_TRUST) >= TRUST_FLOOR]
        self.world.bus.publish(Event(
            "sensor_integrity", ev.house_id, ev.entity_id,
            {
                "advisory": True,
                "violated": violated,
                "reasons": [r.because for r in self.couplings if r.name in violated],
                "untrusted": sorted(newly_untrusted),
                "trust": {e: round(self.score(self._fq(ev.house_id, e)), 3)
                          for e in _watched_relatives(self.couplings)},
            },
            ev.tick,
        ))


def _watched_relatives(couplings: tuple[CouplingRule, ...]) -> list[str]:
    seen: list[str] = []
    for r in couplings:
        for s in r.sensors:
            if s not in seen:
                seen.append(s)
    return seen
