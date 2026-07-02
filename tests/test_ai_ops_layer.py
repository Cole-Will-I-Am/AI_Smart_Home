"""Offline test of the Claude ops layer using a scripted mock client (no network).

The whole point: whatever Claude proposes, the permission engine is the thing that decides.
"""
import os
import pytest
from homeops.ai import OpsLayer


class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class MockMessages:
    def __init__(self, script):
        self.script = script
        self.i = 0

    def create(self, **kwargs):
        r = self.script[self.i]
        self.i += 1
        return r


class MockClient:
    def __init__(self, script):
        self.messages = MockMessages(script)


def tool_use(name, inp, tid="t1"):
    return Blk(type="tool_use", id=tid, name=name, input=inp)


def text(t):
    return Blk(type="text", text=t)


def test_ai_proposes_L1_and_engine_executes(world):
    client = MockClient([
        Resp([tool_use("propose_command",
                       {"house_id": "house_a", "subsystem": "light", "target": "living_room", "action": "turn_on"})]),
        Resp([text("Turned on the living-room light.")], stop_reason="end_turn"),
    ])
    out = OpsLayer(world, client=client).run("turn on the living room", "house_a")
    assert world.state.get_state("house_a.light.living_room") == "on"
    assert out["actions"][0]["status"] == "executed"


def test_ai_proposes_L4_and_engine_refuses(world):
    client = MockClient([
        Resp([tool_use("propose_command",
                       {"house_id": "house_a", "subsystem": "lock", "target": "front_door", "action": "unlock_unknown"})]),
        Resp([text("That needs a human; I recommended it instead.")], stop_reason="end_turn"),
    ])
    out = OpsLayer(world, client=client).run("let the stranger in", "house_a")
    assert out["actions"][0]["status"] == "recommend_only"
    assert world.state.get_state("house_a.lock.front_door") == "locked"   # not unlocked


def test_ai_L2_returns_confirm_required(world):
    client = MockClient([
        Resp([tool_use("propose_command",
                       {"house_id": "house_a", "subsystem": "lock", "target": "front_door", "action": "unlock"})]),
        Resp([text("Awaiting human confirmation.")], stop_reason="end_turn"),
    ])
    out = OpsLayer(world, client=client).run("unlock the front door", "house_a")
    assert out["actions"][0]["status"] == "confirm_required"


@pytest.mark.live
def test_live_smoke():
    """Optional: one real Claude round-trip. Run with: pytest -m live (needs ANTHROPIC_API_KEY)."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("no ANTHROPIC_API_KEY")
    import anthropic
    from homeops import build_world
    world = build_world()
    out = OpsLayer(world, client=anthropic.Anthropic()).run(
        "It's night. Make sure House A's exterior front light is on.", "house_a")
    assert "actions" in out
