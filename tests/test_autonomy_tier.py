from datetime import date, datetime

import pytest

from homeops.ai.ops_layer import OpsLayer
from homeops.baseline import SLOTS
from homeops.delegations import Delegation
from homeops.events import Event
from homeops.permissions import Operator
from homeops.routines import Routine
from homeops.simulator import scenarios

OWNER = Operator("owner", "house_a", name="colton")


def at(hour=12, day=15):
    return lambda: datetime(2026, 1, day, hour, 0)


def install_ready_routine(world, routine_id, steps, **kw):
    world.routines.clock = at(12)
    r = Routine(
        id=routine_id,
        when={"entity_id": "house_a.light.kitchen", "equals": "off"},
        then_steps=steps,
        grantor="colton",
        house_id="house_a",
        **kw,
    )
    return world.routines.install(r, OWNER)


def test_composite_inference_is_typed_advisory_and_never_actuates(world):
    world.state.set_state("house_a.sensor.flow_meter", 42.0)
    before = len(world.audit.records)

    world.bus.publish(Event(
        "anomaly",
        "house_a",
        "house_a.sensor.pressure",
        {"metric": "value", "value": 40.0, "expected": 60.0, "z": 9.0, "n": 30},
        world.engine.tick,
    ))

    inferences = [e for e in world.bus.history if e.type == "inference"]
    assert inferences
    assert inferences[-1].data["inference_type"] == "leak_suspected"
    assert inferences[-1].data["advisory"] is True
    assert any("leak_suspected" in n["message"] for n in world.notifications)
    assert len(world.audit.records) == before
    assert world.state.get_state("house_a.water.main_valve") == "open"


def test_routine_fires_and_l1_step_executes_through_router(bare):
    install_ready_routine(
        bare,
        "rt-l1-light",
        [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
    )

    bare.routines.evaluate_tick()

    assert bare.state.get_state("house_a.light.kitchen") == "on"
    assert any(r.status == "executed" and r.subsystem == "light" for r in bare.audit.records)
    assert any(r.status == "routine_fired" and r.target == "rt-l1-light" for r in bare.audit.records)


def test_routine_l2_without_delegation_queues_confirmation_and_does_not_execute(bare):
    install_ready_routine(
        bare,
        "rt-l2-arm",
        [{"subsystem": "alarm", "target": "panel", "action": "arm", "args": {"mode": "night"}}],
    )

    bare.routines.evaluate_tick()

    assert bare.state.get_state("house_a.alarm.panel") == "disarmed"
    assert any(r.status == "confirm_required" and r.subsystem == "alarm" for r in bare.audit.records)
    pending = OpsLayer(bare)._run_tool("list_pending_confirmations", {"house_id": "house_a"}, "house_a")
    assert any("alarm.panel arm" in p["description"] for p in pending["pending"])


def test_routine_l3_with_covering_standing_delegation_executes(bare):
    bare.delegations.grant(
        Delegation("d-battery", "colton", "house_a", "battery", "set_mode"),
        OWNER,
    )
    install_ready_routine(
        bare,
        "rt-l3-battery",
        [{"subsystem": "battery", "target": "main", "action": "set_mode", "args": {"mode": "backup"}}],
    )

    bare.routines.evaluate_tick()

    assert bare.state.get_state("house_a.battery.main") == "backup"
    assert any(r.status == "delegated" and r.target == "d-battery" for r in bare.audit.records)


def test_only_owner_installs_and_ai_can_only_propose_routine_specs(bare):
    ops = OpsLayer(bare)
    out = ops._run_tool(
        "propose_routine",
        {"house_id": "house_a",
         "when": {"entity_id": "house_a.light.kitchen", "equals": "off"},
         "authority_max_level": 2,
         "then_steps": [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}]},
        "house_a",
    )
    assert out["status"] == "routine_spec"
    assert out["installed"] is False
    assert out["spec"]["install_requires"] == "human_owner"
    assert out["spec"]["authority_max_level"] == 2
    assert len(bare.routines) == 0

    with pytest.raises(PermissionError):
        bare.routines.install(
            Routine("rt-ai", out["spec"]["when"], out["spec"]["then_steps"], "ai-ops", "house_a",
                    authority_max_level=out["spec"]["authority_max_level"]),
            Operator("ai", "house_a", "ai-ops"),
        )

    bare.routines.install(
        Routine("rt-owner", out["spec"]["when"], out["spec"]["then_steps"], "colton", "house_a",
                authority_max_level=out["spec"]["authority_max_level"]),
        OWNER,
    )
    listed = ops._run_tool("list_routines", {"house_id": "house_a"}, "house_a")
    assert listed["routines"][0]["id"] == "rt-owner"
    assert listed["routines"][0]["authority_max_level"] == 2
    assert listed["routines"][0]["budget_remaining"] == listed["routines"][0]["budget_per_day"]


def test_l4_and_l5_actions_are_rejected_at_routine_install(bare):
    for idx, step in enumerate([
        {"subsystem": "power", "target": "panel", "action": "main_breaker"},
        {"subsystem": "safety", "target": "panel", "action": "bypass"},
    ]):
        with pytest.raises(ValueError):
            install_ready_routine(bare, f"rt-forbidden-{idx}", [step])


def test_revoked_expired_and_over_budget_routines_do_not_fire(bare):
    revoked = install_ready_routine(
        bare,
        "rt-revoked",
        [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
    )
    bare.routines.revoke(revoked.id)
    bare.routines.evaluate_tick()
    assert bare.state.get_state("house_a.light.kitchen") == "off"

    bare.routines.clock = at(12, day=15)
    bare.routines.install(Routine(
        "rt-expired",
        {"entity_id": "house_a.light.kitchen", "equals": "off"},
        [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
        "colton",
        "house_a",
        expires=date(2026, 1, 10),
    ), OWNER)
    bare.routines.evaluate_tick()
    assert bare.state.get_state("house_a.light.kitchen") == "off"

    one_shot = install_ready_routine(
        bare,
        "rt-budget",
        [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
        budget_per_day=1,
    )
    bare.routines.evaluate_tick()
    assert bare.state.get_state("house_a.light.kitchen") == "on"
    bare.state.set_state("house_a.light.kitchen", "off")
    bare.routines.evaluate_tick()
    assert bare.state.get_state("house_a.light.kitchen") == "off"
    assert one_shot.used_today == 1


def test_advisory_inference_event_can_trigger_a_routine_action(bare):
    bare.routines.clock = at(12)
    bare.routines.install(Routine(
        "rt-ventilation-advisory",
        {"event_type": "inference", "inference_type": "ventilation_fault"},
        [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
        "colton",
        "house_a",
    ), OWNER)
    bare.routines.install(Routine(
        "rt-direct-ventilation-advisory",
        {"recent_event": {"type": "ventilation_fault"}},
        [{"subsystem": "light", "target": "living_room", "action": "turn_on"}],
        "colton",
        "house_a",
    ), OWNER)

    bare.bus.publish(Event(
        "inference",
        "house_a",
        "house_a.sensor.co2",
        {"inference_type": "ventilation_fault", "advisory": True},
        bare.engine.tick,
    ))
    bare.bus.publish(Event(
        "ventilation_fault",
        "house_a",
        "house_a.sensor.co2",
        {"advisory": True},
        bare.engine.tick,
    ))

    assert bare.state.get_state("house_a.light.kitchen") == "on"
    assert bare.state.get_state("house_a.light.living_room") == "on"
    assert any(r.status == "routine_fired" and r.target == "rt-ventilation-advisory"
               for r in bare.audit.records)
    assert any(r.status == "routine_fired" and r.target == "rt-direct-ventilation-advisory"
               for r in bare.audit.records)


def test_routine_carried_authority_executes_delegable_l3_but_not_safety_critical(bare):
    assert len(bare.delegations) == 0
    install_ready_routine(
        bare,
        "rt-inline-authority",
        [
            {"subsystem": "battery", "target": "main", "action": "set_mode",
             "args": {"mode": "backup"}},
            {"subsystem": "generator", "target": "main", "action": "start"},
        ],
        authority_max_level=3,
    )

    out = bare.routines.evaluate()

    results = out[0]["results"]
    assert results[0]["status"] == "executed"
    assert results[0]["delegation"] == "routine:rt-inline-authority:authority"
    assert results[1]["status"] == "confirm_required"
    assert "delegation" not in results[1]
    assert bare.state.get_state("house_a.battery.main") == "backup"
    assert bare.state.get_state("house_a.generator.main") == "off"
    assert any(r.status == "delegated" and r.target == "routine:rt-inline-authority:authority"
               for r in bare.audit.records)


def test_routine_authority_cap_is_bounded_by_l3_and_installer_role(bare):
    with pytest.raises(ValueError):
        install_ready_routine(
            bare,
            "rt-cap-too-high",
            [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
            authority_max_level=4,
        )

    limited_owner = Operator("owner", "house_a", name="colton", max_level=2)
    with pytest.raises(PermissionError):
        bare.routines.install(Routine(
            "rt-cap-above-role",
            {"entity_id": "house_a.light.kitchen", "equals": "off"},
            [{"subsystem": "light", "target": "kitchen", "action": "turn_on"}],
            "colton",
            "house_a",
            authority_max_level=3,
        ), limited_owner)


def test_emergency_system_path_still_bypasses_routine_delegation_requirements(world):
    scenarios.leak(world, "house_a")
    world.tick(2)

    assert world.state.get_state("house_a.water.main_valve") == "closed"
    assert any(r.status == "executed" and r.subsystem == "water" and r.action == "shutoff_main"
               for r in world.audit.records)


def test_trend_tool_reads_vigilance_buckets(world):
    ent = "house_a.sensor.pressure"
    for i in range(30):
        world.bus.publish(Event("telemetry", "house_a", ent, {"value": 100.0 + i}, tick=7 + SLOTS * i))
    world.state.set_state(ent, 140)

    out = OpsLayer(world)._run_tool("trend", {"house_id": "house_a", "entity_id": ent}, "house_a")

    assert out["status"] == "ok"
    assert out["samples"] >= 24
    assert out["direction"] == "rising"
    assert out["slope"] > 0
