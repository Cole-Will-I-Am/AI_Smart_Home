"""Tests for the three next-level tiers: sensor-integrity (causal consistency),
runtime conformance monitor, and the advisory counterfactual gate.

Each tier is a derived, read-only reducer that never actuates; these tests pin exactly that
property plus the security behaviour that motivates the tier. They run under the same
in-process world the rest of the suite uses.
"""
import pytest

from homeops import build_world
from homeops.events import Event
from homeops.permissions import Intent, Operator
from homeops.director import DirectorState
from homeops.sensor_integrity import TRUST_FLOOR


@pytest.fixture
def w():
    return build_world()


def _set(w, rel, value, **attrs):
    w.state.set_state(f"house_a.{rel}", value, **attrs)


# ------------------------------------------------------------------ sensor integrity

def test_consistent_flow_pressure_keeps_trust_high(w):
    """A real high-flow event pulls pressure down -> coupling holds -> flow stays trusted."""
    _set(w, "sensor.flow_meter", 45.0)
    _set(w, "sensor.pressure", 30.0)          # pressure dropped, as physics demands
    w.integrity.evaluate("house_a")
    assert w.integrity.trusts("house_a.sensor.flow_meter")
    assert w.integrity.score("house_a.sensor.flow_meter") >= TRUST_FLOOR


def test_spoofed_flow_flat_pressure_loses_trust(w):
    """Flow pinned high while pressure never moves is physically impossible -> trust falls."""
    _set(w, "sensor.pressure", 60.0)          # nominal, flat
    for _ in range(3):
        _set(w, "sensor.flow_meter", 45.0)    # high flow, but pressure stays flat
        w.integrity.evaluate("house_a")
    assert not w.integrity.trusts("house_a.sensor.flow_meter")
    assert not w.integrity.trusts("house_a.sensor.pressure")


def test_untrusted_flow_blocks_two_signal_shutoff(w):
    """The security payoff: a spoofed flow sensor can no longer satisfy the two-signal rule,
    so the destructive main-shutoff is withheld and the house escalates to a human instead."""
    # Establish the spoof: high flow, flat pressure, over several ticks -> flow untrusted.
    _set(w, "sensor.pressure", 60.0)
    for _ in range(3):
        _set(w, "sensor.flow_meter", 45.0)
        w.integrity.evaluate("house_a")
    assert not w.integrity.trusts("house_a.sensor.flow_meter")

    # Now drive a leak event with a genuinely wet sensor: both raw signals are "present",
    # but integrity has withdrawn trust from the flow channel.
    _set(w, "sensor.leak_kitchen", "wet")
    valve_before = w.state.get_state("house_a.water.main_valve")
    w.bus.publish(Event("leak", "house_a", "house_a.sensor.leak_kitchen", {}, tick=w.engine.tick))

    assert w.state.get_state("house_a.water.main_valve") == valve_before  # NOT closed
    assert w.state.get_state("house_a.water.main_valve") == "open"
    assert w.director.state("house_a") != DirectorState.AUTONOMOUS        # escalated
    assert any("integrity is compromised" in n["message"] for n in w.notifications)


def test_trusted_signals_still_close_the_main(w):
    """Control case: when both signals are trusted (flow high AND pressure dropped), the
    two-signal shutoff fires exactly as before — the gate only blocks the impossible case."""
    _set(w, "sensor.pressure", 28.0)          # consistent pressure drop
    _set(w, "sensor.flow_meter", 45.0)
    w.integrity.evaluate("house_a")
    assert w.integrity.trusts("house_a.sensor.flow_meter")

    _set(w, "sensor.leak_kitchen", "wet")
    w.bus.publish(Event("leak", "house_a", "house_a.sensor.leak_kitchen", {}, tick=w.engine.tick))
    # the sim models valve travel: actuation shows as "closing", then "closed" after ticks
    assert w.state.get_state("house_a.water.main_valve") == "closing"
    assert any(r.status == "executed" and r.subsystem == "water" and r.action == "shutoff_main"
               for r in w.audit.records)


def test_integrity_never_actuates(w):
    """The tier holds no authority: evaluating it changes no device state, only trust memory."""
    before = {eid: e.state for h in w.houses.values() for eid, e in h.entities.items()}
    _set(w, "sensor.pressure", 60.0)
    _set(w, "sensor.flow_meter", 45.0)
    w.integrity.evaluate("house_a")
    after = {eid: e.state for h in w.houses.values() for eid, e in h.entities.items()}
    # only the two sensor values we set ourselves changed; the tier moved nothing
    changed = {k for k in before if before[k] != after[k]}
    assert changed <= {"house_a.sensor.pressure", "house_a.sensor.flow_meter"}


# ------------------------------------------------------------------ conformance monitor

def test_fire_egress_obligation_discharged_on_healthy_house(w):
    """A verified fire opens the egress obligation; the local automation unlocks egress the
    same tick, so the obligation discharges and no violation fires."""
    w.bus.publish(Event("smoke_co", "house_a", "house_a.sensor.smoke_co_hall",
                        {"verified": True}, tick=w.engine.tick))
    assert w.state.get_state("house_a.lock.egress_side") == "unlocked"
    assert not any(v["invariant"] == "INV-FIRE-EGRESS" for v in w.conformance.violations)


def test_fire_egress_violation_when_response_missing(w):
    """If the egress unlock never happens, the bounded-response invariant fires once the
    deadline passes, records a hash-chained incident, and escalates the Director."""
    cm = w.conformance
    # open the obligation directly, bypassing the automation that would satisfy it
    cm._open("INV-FIRE-EGRESS", "house_a", {"cause": "test: response suppressed"})
    # advance past the deadline; egress stays locked
    for _ in range(5):
        w.tick()
    fired = [v for v in cm.violations if v["invariant"] == "INV-FIRE-EGRESS"]
    assert fired, "expected an INV-FIRE-EGRESS violation once the deadline lapsed"
    assert any(r.status == "conformance_violation" for r in w.audit.records)
    # the breach requested a Director escalation (recorded as a mode transition away from
    # AUTONOMOUS); the live mode may later time out, so we assert the transition happened
    assert any(r.status == "director_transition" and r.args.get("from") == "autonomous"
               for r in w.audit.records)


def test_ai_l4_execution_would_be_caught(w):
    """INV-AI-L4 is a standing invariant. We can't get an L4 through the engine (there is no
    path — that's the point), so we witness the monitor's detector directly: an executed AI
    record at level>=4 is classified as a breach."""
    from homeops.audit import AuditRecord
    w.audit.record(AuditRecord(
        tick=w.engine.tick, operator="ai", house_id="house_a", subsystem="power",
        target="panel", action="main_breaker", args={}, level=4, status="executed",
        message="synthetic: an L4 that must never occur"))
    fired = w.conformance.evaluate()
    assert any(v["invariant"] == "INV-AI-L4" for v in fired)


def test_conformance_never_actuates(w):
    """The monitor's only effects are an incident record + a Director escalation request;
    it moves no device."""
    before = {eid: e.state for h in w.houses.values() for eid, e in h.entities.items()}
    w.conformance._open("INV-LEAK-MAIN", "house_a", {"cause": "test"})
    for _ in range(5):
        w.tick()
    after = {eid: e.state for h in w.houses.values() for eid, e in h.entities.items()}
    # the water main is not touched by the monitor (it may be escalated, not actuated)
    assert before["house_a.water.main_valve"] == after["house_a.water.main_valve"]


# ------------------------------------------------------------------ counterfactual gate

def test_shed_below_reserve_is_flagged(w):
    """A load-shed that the forward model predicts will cross the battery reserve is BLOCKED
    in the advisory verdict."""
    e = w.state.entity("house_a.battery.main")
    e.attributes["soc"] = 25.0                # just above the 20% reserve
    pred = w.predictive.assess("house_a",
                               {"subsystem": "power", "target": "load_shed", "action": "load_shed", "args": {}})
    assert not pred.allow
    assert "battery_reserve_preserved" in pred.violated
    assert min(pred.trajectory["battery_soc"]) < 20.0


def test_safe_action_is_allowed(w):
    """A benign in-range action the model sees as harmless is ALLOWed."""
    pred = w.predictive.assess("house_a",
                               {"subsystem": "light", "target": "kitchen", "action": "turn_on", "args": {}})
    assert pred.allow and not pred.violated


def test_egress_lock_during_fire_is_flagged(w):
    """With fire inferred, locking egress is predicted to leave an egress path locked -> BLOCK."""
    _set(w, "sensor.smoke_co_hall", "detected")   # fire_inferred callback now true
    pred = w.predictive.assess("house_a",
                               {"subsystem": "lock", "target": "egress_side", "action": "lock", "args": {}})
    assert not pred.allow
    assert "egress_open_while_fire" in pred.violated


def test_gate_is_advisory_not_enforcing(w):
    """The gate defaults to shadow mode and is not consulted by the router: a BLOCK verdict
    does not stop the engine from executing the same action."""
    assert w.predictive.enforcing is False
    e = w.state.entity("house_a.battery.main")
    e.attributes["soc"] = 25.0
    pred = w.predictive.assess("house_a",
                               {"subsystem": "power", "target": "load_shed", "action": "load_shed", "args": {}})
    assert not pred.allow                       # advisory says block
    # engine still executes it (system/emergency operator), proving the gate isn't in the path
    r = w.router.execute(
        Intent("house_a", "power", "load_shed", "load_shed", {"tier": "nonessential"}, emergency=True),
        Operator(kind="system", active_house="house_a", name="test"))
    assert r.ok


def test_validation_harness_quantifies_error(w):
    """The promotion harness reports model error and gates trustworthiness on it."""
    good = w.predictive.validate([{"predicted": 50.0, "observed": 50.4},
                                  {"predicted": 48.0, "observed": 47.6}])
    assert good["trustworthy"] and good["mae"] <= 1.0
    bad = w.predictive.validate([{"predicted": 50.0, "observed": 60.0}])
    assert not bad["trustworthy"]


# ------------------------------------------------------------------ escalation-evidence guard
# Regression guard for the integration bug the tiers surfaced: Director.escalate() silently
# rejects any evidence that validate_for_trigger() would fail, so a call site that omits the
# required discriminator (e.g. a LIFE_SAFETY_INFERENCE without a valid "kind") is a silent
# no-op. These tests pin that EVERY real escalation path produces accepted evidence.

def test_conformance_escalation_evidence_is_accepted(w):
    """The conformance monitor's escalation must satisfy validate_for_trigger (carry a
    life-safety kind), or the Director would ignore it."""
    from homeops.director import SemanticDelta, validate_for_trigger
    captured = {}
    orig = w.director.escalate
    def spy(house_id, trigger, evidence):
        captured["ev"] = evidence
        captured["ok"] = validate_for_trigger(
            SemanticDelta("t", trigger, house_id, 0, dict(evidence)))
        return orig(house_id, trigger, evidence)
    w.director.escalate = spy
    w.conformance._open("INV-LEAK-MAIN", "house_a", {"cause": "test"})
    for _ in range(5):
        w.tick()
    assert captured.get("ok") is True, f"conformance escalation evidence rejected: {captured.get('ev')}"


def test_leak_gate_escalation_evidence_is_accepted(w):
    """The sensor-integrity leak gate's escalation must likewise be accepted."""
    from homeops.director import SemanticDelta, validate_for_trigger
    # spoof the flow channel untrusted
    _set(w, "sensor.pressure", 60.0)
    for _ in range(3):
        _set(w, "sensor.flow_meter", 45.0)
        w.integrity.evaluate("house_a")
    captured = {}
    orig = w.director.escalate
    def spy(house_id, trigger, evidence):
        captured["ok"] = validate_for_trigger(
            SemanticDelta("t", trigger, house_id, 0, dict(evidence)))
        captured["ev"] = evidence
        return orig(house_id, trigger, evidence)
    w.director.escalate = spy
    _set(w, "sensor.leak_kitchen", "wet")
    w.bus.publish(Event("leak", "house_a", "house_a.sensor.leak_kitchen", {}, tick=w.engine.tick))
    assert captured.get("ok") is True, f"leak-gate escalation evidence rejected: {captured.get('ev')}"


def test_all_static_escalation_callsites_have_required_discriminator():
    """Static guard: for every escalate() call whose Trigger requires a *discriminator key*
    (MANUAL_ESCALATE -> 'by'; LIFE_SAFETY_INFERENCE -> a valid 'kind'), assert the evidence
    literal supplies it. Numeric-threshold triggers (HEALTH_CASCADE / ACTUATION_FAILURE_RATE)
    carry runtime values a literal can't witness and are covered by the runtime tests above.
    This catches a new life-safety call site that forgets 'kind' without executing it."""
    import ast
    import pathlib
    from homeops.director import LIFE_SAFETY_KINDS

    required = {"LIFE_SAFETY_INFERENCE": "kind", "MANUAL_ESCALATE": "by"}
    root = pathlib.Path(__file__).resolve().parent.parent / "homeops"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "escalate" and len(node.args) >= 3
                    and isinstance(node.args[2], ast.Dict)
                    and isinstance(node.args[1], ast.Attribute)):
                continue
            trig = node.args[1].attr
            if trig not in required:
                continue
            key = required[trig]
            keys = {k.value for k in node.args[2].keys if isinstance(k, ast.Constant)}
            if key not in keys:
                offenders.append((str(path.relative_to(root.parent)), node.lineno, trig, key))
                continue
            # for LIFE_SAFETY_INFERENCE, also check any *constant* kind is a valid one
            if trig == "LIFE_SAFETY_INFERENCE":
                for k, v in zip(node.args[2].keys, node.args[2].values):
                    if isinstance(k, ast.Constant) and k.value == "kind":
                        consts = ([v.value] if isinstance(v, ast.Constant)
                                  else [b.value for b in (getattr(v, "body", None), getattr(v, "orelse", None))
                                        if isinstance(b, ast.Constant)])
                        for c in consts:
                            if c not in LIFE_SAFETY_KINDS:
                                offenders.append((str(path.relative_to(root.parent)),
                                                  node.lineno, trig, f"kind={c!r} not in LIFE_SAFETY_KINDS"))
    assert not offenders, "escalate() call sites missing a required discriminator:\n" + \
        "\n".join(f"  {p}:{ln}  {t}  needs {k}" for p, ln, t, k in offenders)
