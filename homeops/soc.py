"""Home SOC — operational intelligence over the estate.

A security-operations view for a residence: not new control, but *inference* over the three
streams the engine already emits — the tamper-evident audit log, the health registry, and live
entity state. Every function here is a pure reducer: no new persistent state, no I/O, no actuation.
That purity is the point — operational intelligence must never itself become an actor with
authority, so the SOC can only read and reason, never command.

Four analytics, each answering a question a high-value-property owner actually asks:
  overnight_diff   — "what changed while I was away?"     (state snapshot delta)
  health_drift     — "which devices are trending to fail?" (staleness gradient)
  readiness        — "is the property actually armed?"     (safety-subsystem scoring)
  correlate        — "what happened, as incidents?"        (audit clustering by house+window)
  situation_report — the composite the dashboard/API renders.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Subsystems whose readiness constitutes "the property is protected".
# Each maps to (entity-name substring or None, the state that means "safe/armed").
READINESS_SPEC: dict[str, dict] = {
    "water_main":   {"subsystem": "water",     "name_hint": "main_valve", "safe": {"open"}, "invert": False,
                     "meaning": "water main controllable"},
    "leak_mesh":    {"subsystem": "sensor",    "name_hint": "leak",       "safe": {"dry"},  "invert": False,
                     "meaning": "leak sensors dry"},
    "smoke_co":     {"subsystem": "sensor",    "name_hint": "smoke_co",   "safe": {"clear"}, "invert": False,
                     "meaning": "smoke/CO clear"},
    "locks":        {"subsystem": "lock",      "name_hint": None,         "safe": {"locked"}, "invert": False,
                     "meaning": "perimeter locked"},
    "generator":    {"subsystem": "generator", "name_hint": None,         "safe": {"ready", "standby", "off"}, "invert": False,
                     "meaning": "generator ready"},
}


# ---- 1. state snapshot & overnight diff --------------------------------------------
def snapshot(world, house_id: str) -> dict[str, object]:
    """A flat {entity_id: state} snapshot — the atom the diff compares."""
    h = world.houses[house_id]
    return {e.entity_id: e.state for e in h.entities.values()}


@dataclass
class StateDelta:
    entity_id: str
    subsystem: str
    before: object
    after: object
    safety_relevant: bool


def overnight_diff(before: dict, after: dict, world=None, house_id: str | None = None) -> list[StateDelta]:
    """Compare two snapshots. If a world+house is given, tag safety-relevant subsystems so the
    UI can foreground 'the main valve closed overnight' over 'a lamp turned off'."""
    safety_subs = {"water", "lock", "alarm", "generator", "sensor"}
    deltas: list[StateDelta] = []
    for eid in sorted(set(before) | set(after)):
        b, a = before.get(eid), after.get(eid)
        if b == a:
            continue
        sub = eid.split(".")[1] if "." in eid else ""
        deltas.append(StateDelta(eid, sub, b, a, sub in safety_subs))
    return deltas


# ---- 2. health drift --------------------------------------------------------------
@dataclass
class DriftRecord:
    entity_id: str
    status: str            # ok | stale | offline | unknown
    ticks_since_seen: int | None
    fraction_of_window: float | None    # how far toward "stale" (>=1.0 means past the window)


def health_drift(world, house_id: str) -> list[DriftRecord]:
    """Per-device staleness gradient. A device at 0.8 of its window is 'drifting' — visible before
    it crosses into stale/offline. This is the 'device health drift' the SOC brief asks for."""
    h = world.houses[house_id]
    now = world.engine.tick
    reg = world.health
    win = getattr(reg, "window", 30) or 30
    out: list[DriftRecord] = []
    for e in h.entities.values():
        status = reg.status(e.entity_id, now)
        last = reg._last_seen.get(e.entity_id)
        since = (now - last) if last is not None else None
        frac = (since / win) if since is not None else None
        out.append(DriftRecord(e.entity_id, status, since, round(frac, 3) if frac is not None else None))
    # worst first: offline, then most-drifted
    order = {"offline": 0, "stale": 1, "unknown": 2, "ok": 3}
    out.sort(key=lambda d: (order.get(d.status, 9), -(d.fraction_of_window or 0)))
    return out


def drifting(world, house_id: str, threshold: float = 0.66) -> list[DriftRecord]:
    """Devices not yet stale but past `threshold` of their window — the early-warning set."""
    return [d for d in health_drift(world, house_id)
            if d.status == "ok" and (d.fraction_of_window or 0) >= threshold]


# ---- 3. readiness scoring ----------------------------------------------------------
@dataclass
class ReadinessItem:
    key: str
    meaning: str
    ready: bool
    detail: str


@dataclass
class ReadinessReport:
    house_id: str
    items: list[ReadinessItem] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.items:
            return 1.0
        return round(sum(1 for i in self.items if i.ready) / len(self.items), 3)

    @property
    def armed(self) -> bool:
        return all(i.ready for i in self.items)


def readiness(world, house_id: str) -> ReadinessReport:
    """Score whether each safety subsystem is in its safe/armed state AND health-visible.
    A subsystem whose devices are offline is NOT ready even if last-known state was safe —
    readiness requires both a safe state and a live device (fail-closed)."""
    h = world.houses[house_id]
    now = world.engine.tick
    rep = ReadinessReport(house_id)
    for key, spec in READINESS_SPEC.items():
        ents = [e for e in h.entities.values()
                if e.subsystem == spec["subsystem"]
                and (spec["name_hint"] is None or spec["name_hint"] in e.name)]
        if not ents:
            continue
        unsafe = [e for e in ents if e.state not in spec["safe"]]
        offline = [e for e in ents if world.health.status(e.entity_id, now) == "offline"]
        ready = not unsafe and not offline
        if offline:
            detail = f"{len(offline)} device(s) offline — cannot confirm"
        elif unsafe:
            detail = f"{len(unsafe)} not in safe state: {[e.name + '=' + str(e.state) for e in unsafe][:3]}"
        else:
            detail = f"all {len(ents)} nominal"
        rep.items.append(ReadinessItem(key, spec["meaning"], ready, detail))
    return rep


# ---- 4. incident correlation -------------------------------------------------------
@dataclass
class Incident:
    house_id: str
    start_tick: int
    end_tick: int
    records: list = field(default_factory=list)   # AuditRecord list
    subsystems: set = field(default_factory=set)

    @property
    def span(self) -> int:
        return self.end_tick - self.start_tick

    @property
    def had_refusal(self) -> bool:
        return any(r.status in ("refused", "prohibited", "recommend_only") for r in self.records)

    def summary(self) -> str:
        acts = ", ".join(sorted({f"{r.subsystem}.{r.action}" for r in self.records}))[:120]
        return (f"[{self.house_id}] t{self.start_tick}-{self.end_tick}: "
                f"{len(self.records)} actions across {sorted(self.subsystems)} — {acts}")


def correlate(world, house_id: str | None = None, window: int = 2,
              interesting_only: bool = True) -> list[Incident]:
    """Cluster audit records into incidents: consecutive records (optionally for one house) whose
    ticks fall within `window` of each other. Turns a flat log into 'what happened' — the
    correlation the SOC view sells. `interesting_only` drops singleton routine actions."""
    recs = [r for r in world.audit.records
            if house_id is None or r.house_id == house_id]
    recs = [r for r in recs if r.subsystem not in ("advisory",)]
    recs.sort(key=lambda r: r.tick)
    incidents: list[Incident] = []
    cur: Incident | None = None
    for r in recs:
        if cur is not None and r.house_id == cur.house_id and r.tick - cur.end_tick <= window:
            cur.records.append(r); cur.end_tick = r.tick; cur.subsystems.add(r.subsystem)
        else:
            if cur is not None:
                incidents.append(cur)
            cur = Incident(r.house_id, r.tick, r.tick, [r], {r.subsystem})
    if cur is not None:
        incidents.append(cur)
    if interesting_only:
        incidents = [i for i in incidents
                     if len(i.records) > 1 or i.had_refusal or i.subsystems & {"water", "alarm", "generator", "lock"}]
    return incidents


# ---- composite situation report ----------------------------------------------------
def situation_report(world, house_id: str, prior_snapshot: dict | None = None) -> dict:
    """The one object a Home-SOC dashboard or API renders."""
    rep = readiness(world, house_id)
    drift = health_drift(world, house_id)
    incidents = correlate(world, house_id)
    diff = overnight_diff(prior_snapshot, snapshot(world, house_id), world, house_id) if prior_snapshot else []
    audit_ok, _ = world.audit.verify_chain()
    return {
        "house_id": house_id,
        "armed": rep.armed,
        "readiness_score": rep.score,
        "readiness": [(i.key, i.ready, i.detail) for i in rep.items],
        "offline_devices": [d.entity_id for d in drift if d.status == "offline"],
        "drifting_devices": [(d.entity_id, d.fraction_of_window) for d in drift
                             if d.status == "ok" and (d.fraction_of_window or 0) >= 0.66],
        "incidents": [i.summary() for i in incidents],
        "overnight_changes": [(d.entity_id, d.before, d.after, d.safety_relevant) for d in diff],
        "audit_intact": audit_ok,
    }
