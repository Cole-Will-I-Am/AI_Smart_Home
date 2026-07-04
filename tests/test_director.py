"""Tests for the House Director — operating-mode FSM + escalation detector."""
from homeops.bootstrap import build_world
from homeops.permissions import Operator
from homeops.director import DirectorState, Trigger
from homeops.events import Event
from homeops.routines import Routine

OWNER = Operator("owner", "house_a", "colton", houses="*")
AI = Operator("ai", "house_a")


def _w():
    return build_world()


def test_default_mode_is_autonomous_per_house():
    w = _w()
    assert w.director.state("house_a") is DirectorState.AUTONOMOUS
    assert w.director.state("house_b") is DirectorState.AUTONOMOUS


def test_manual_escalate_and_de_escalate_are_audited():
    w = _w()
    d = w.director
    before = len(w.audit.records)
    assert d.manual_escalate("house_a", OWNER) is True
    assert d.state("house_a") is DirectorState.AI_ACTIVE
    assert d.de_escalate("house_a") is True
    assert d.state("house_a") is DirectorState.AUTONOMOUS
    trans = [r for r in w.audit.records[before:] if r.status == "director_transition"]
    assert len(trans) == 2
    assert w.audit.verify_chain()[0] is True          # I5: chain still verifies


def test_life_safety_inference_escalates():
    w = _w()
    w.bus.publish(Event("inference", "house_a", "house_a.sensor.co2",
                        {"inference_type": "ventilation_fault", "advisory": True}, w.engine.tick))
    assert w.director.state("house_a") is DirectorState.AI_ACTIVE


def test_spurious_escalation_does_not_change_mode():          # I3
    w = _w()
    d = w.director
    assert d.escalate("house_a", Trigger.ACTUATION_FAILURE_RATE, {"failures": 0}) is False
    assert d.escalate("house_a", Trigger.LIFE_SAFETY_INFERENCE, {"kind": "not_a_thing"}) is False
    assert d.state("house_a") is DirectorState.AUTONOMOUS


def test_cooldown_prevents_flapping():                         # I4
    w = _w()
    d = w.director
    assert d.escalate("house_a", Trigger.HEALTH_CASCADE, {"offline_count": 3, "threshold": 2}) is True
    d.de_escalate("house_a")
    # same tick -> within cooldown -> refused
    assert d.escalate("house_a", Trigger.HEALTH_CASCADE, {"offline_count": 3, "threshold": 2}) is False
    assert d.state("house_a") is DirectorState.AUTONOMOUS


def test_human_override_suspends_routines_release_restores():  # I2
    w = _w()
    w.routines.install(Routine("rt", {"entity_id": "house_a.light.kitchen", "equals": "off"},
                               [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
                               "colton", "house_a"), OWNER)

    def fire_attempt():
        w.state.set_state("house_a.light.kitchen", "off")
        w.routines.evaluate()
        return w.state.get_state("house_a.light.kitchen")

    assert fire_attempt() == "on"                              # AUTONOMOUS: routine fires
    w.director.enter_override("house_a", OWNER)
    assert w.houses["house_a"].ai_hold is True
    assert fire_attempt() == "off"                             # HUMAN_OVERRIDE: suspended
    w.director.release_override("house_a", OWNER)
    assert w.houses["house_a"].ai_hold is False
    assert fire_attempt() == "on"                              # released: fires again


def test_per_house_isolation():                                # I6
    w = _w()
    d = w.director
    d.enter_override("house_a", OWNER)
    assert d.state("house_a") is DirectorState.HUMAN_OVERRIDE
    assert d.state("house_b") is DirectorState.AUTONOMOUS
    assert w.houses["house_b"].ai_hold is False


def test_only_a_human_may_override():                          # I7
    w = _w()
    d = w.director
    d.enter_override("house_a", OWNER)
    try:
        d.release_override("house_a", AI)
        assert False
    except PermissionError:
        pass
    assert d.state("house_a") is DirectorState.HUMAN_OVERRIDE  # AI could not release
    try:
        d.enter_override("house_b", AI)
        assert False
    except PermissionError:
        pass


def test_containment_budget_exhaustion_returns_to_autonomous():
    w = _w()
    d = w.director
    d.manual_escalate("house_a", OWNER)
    budget = d.containment("house_a").action_budget
    for _ in range(budget):
        assert d.consume_containment("house_a") is True
    assert d.consume_containment("house_a") is False          # budget spent
    assert d.state("house_a") is DirectorState.AUTONOMOUS


def test_health_cascade_auto_escalates_on_tick():
    w = _w()
    d = w.director
    ids = list(w.houses["house_a"].entities)[:2]
    for eid in ids:
        w.health.mark_offline(eid)
    d.evaluate()
    assert d.state("house_a") is DirectorState.AI_ACTIVE
    assert d.state("house_b") is DirectorState.AUTONOMOUS


def test_director_never_actuates():                            # I1
    w = _w()
    d = w.director
    valve = w.state.get_state("house_a.water.main_valve")
    execed = len([r for r in w.audit.records if r.status == "executed"])
    d.manual_escalate("house_a", OWNER)
    d.enter_override("house_a", OWNER)
    d.release_override("house_a", OWNER)
    assert w.state.get_state("house_a.water.main_valve") == valve           # no device changed
    assert len([r for r in w.audit.records if r.status == "executed"]) == execed  # no actuation
