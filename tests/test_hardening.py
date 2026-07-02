"""Regression tests for the fixes from the external (GPT) review — each nails a specific hole."""
from homeops import build_real_world
from homeops.permissions import Intent, Operator, ACTION_LEVELS
from homeops.model import SUBSYS_ACTIONS
from homeops.events import Event
from homeops.ai import OpsLayer


# --- scripted mock Claude client ---------------------------------------------
class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class MockClient:
    class _M:
        def __init__(self, script):
            self.script, self.i = script, 0

        def create(self, **kw):
            r = self.script[self.i]
            self.i += 1
            return r

    def __init__(self, script):
        self.messages = MockClient._M(script)


# --- Finding 1: AI cannot self-confirm cross-house ---------------------------
def test_ai_cannot_self_confirm_cross_house(world):
    # even if the model smuggles confirm_cross_house into args, the tool/ops layer ignores it
    client = MockClient([
        Resp([Blk(type="tool_use", id="t1", name="propose_command", input={
            "house_id": "house_b", "subsystem": "light", "target": "kitchen", "action": "turn_on",
            "confirm_cross_house": True})]),
        Resp([Blk(type="text", text="done")], stop_reason="end_turn"),
    ])
    out = OpsLayer(world, client=client).run("turn on house B kitchen", "house_a")
    assert out["actions"][0]["status"] == "confirm_required"
    assert world.state.get_state("house_b.light.kitchen") == "off"


# --- Finding 2: tokens bound to args and operator, unguessable ---------------
def test_token_bound_to_args(bare):
    op = Operator("owner", "house_a")
    r1 = bare.router.execute(Intent("house_a", "network", "firewall", "firewall_policy", {"rule": "benign"}), op)
    assert r1.status == "confirm_required" and r1.confirm_token
    # reuse the benign token with a DIFFERENT (destructive) rule -> rejected
    r2 = bare.router.execute(Intent("house_a", "network", "firewall", "firewall_policy",
                                    {"rule": "DESTRUCTIVE"}, confirm_token=r1.confirm_token), op)
    assert r2.status == "confirm_required"


def test_token_bound_to_operator(bare):
    owner = Operator("owner", "house_a")
    r1 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), owner)
    tok = r1.confirm_token
    # a different operator (the AI) cannot use the owner's token
    r2 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock", confirm_token=tok),
                             Operator("ai", "house_a"))
    assert r2.status == "confirm_required" and r2.confirm_token is None
    # the owner still can (token wasn't consumed by the failed AI attempt)
    r3 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock", confirm_token=tok), owner)
    assert r3.status == "executed"


def test_token_not_guessable(bare):
    op = Operator("owner", "house_a")
    r = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock", confirm_token="tok-1"), op)
    assert r.status == "confirm_required"   # sequential guess no longer works


# --- Finding 4: leak needs real state, not just the event payload ------------
def test_leak_ignores_spoofed_event(world):
    # abnormal flow in the payload, but the sensors are DRY -> no shutoff
    world.bus.publish(Event("leak", "house_a", "house_a.sensor.leak_kitchen", {"flow": 99}, 0))
    world.tick(2)
    assert world.state.get_state("house_a.water.main_valve") == "open"


def test_leak_needs_both_independent_signals(world):
    world.state.set_state("house_a.sensor.leak_kitchen", "wet")   # one signal only; flow meter still 0
    world.bus.publish(Event("leak", "house_a", "house_a.sensor.leak_kitchen", {"flow": 99}, 0))
    world.tick(2)
    assert world.state.get_state("house_a.water.main_valve") == "open"


# --- Finding 5: rollback cancels pending physical transitions ----------------
def test_rollback_cancels_pending_valve_transition(bare):
    op = Operator("owner", "house_a")
    r1 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main"), op)
    r2 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main",
                                    confirm_token=r1.confirm_token), op)
    assert r2.status == "executed" and bare.state.get_state("house_a.water.main_valve") == "closing"
    assert bare.router.rollback(r2.rollback_token, op)
    bare.tick(3)
    assert bare.state.get_state("house_a.water.main_valve") == "open"   # did NOT sneak to "closed"


# --- Finding 7: fallback must not run as owner -------------------------------
def test_fallback_runs_ai_limited_not_owner(world):
    world.houses["house_a"].ai_hold = True
    out = OpsLayer(world, client=object()).run("arm night", "house_a")
    assert out["mode"] == "fallback"
    assert world.state.get_state("house_a.alarm.panel") == "disarmed"   # L2 not executed
    assert out["actions"] and out["actions"][0]["status"] == "confirm_required"


# --- Finding 9: rollback + manual override are audited -----------------------
def test_rollback_is_audited(bare):
    op = Operator("owner", "house_a")
    r = bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), op)
    bare.router.rollback(r.rollback_token, op)
    assert any(rec.status == "rollback" for rec in bare.audit.records)


def test_manual_override_is_audited(world):
    world.state.manual_override("house_a.lock.front_door", "locked")
    assert any(rec.status == "manual_override" for rec in world.audit.records)


# --- Finding 11: real two-house isolation fails fast on incomplete map -------
def test_build_real_world_requires_complete_entity_map():
    import pytest
    with pytest.raises(ValueError):
        build_real_world("http://ha:8123", "tok", "https://opn", "k", "s",
                         entity_map={"house_a.lock.front_door": "lock.front_a"})  # incomplete


# --- Finding 10 (cooldown) + coverage nit -----------------------------------
def test_destructive_cooldown_blocks_rapid_repeat(bare):
    op = Operator("owner", "house_a")
    t1 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main"), op)
    bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main",
                               confirm_token=t1.confirm_token), op)   # executes, arms cooldown
    t2 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main"), op)
    r2 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main",
                                    confirm_token=t2.confirm_token), op)
    assert r2.status == "refused" and "cooldown" in r2.message


def test_every_advertised_action_has_a_level():
    for sub, actions in SUBSYS_ACTIONS.items():
        for a in actions:
            assert (sub, a) in ACTION_LEVELS, f"{sub}.{a} missing from ACTION_LEVELS"
