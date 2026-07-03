"""Part 10: resident chat — memory across turns, and a confirm dance in which the token
travels engine -> human -> engine and provably never enters the model's context."""
import json

from homeops.ai.session import ChatSession
from homeops.permissions import Operator


class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class MockMessages:
    def __init__(self, script):
        self.script, self.i, self.calls = script, 0, []

    def create(self, **kwargs):
        self.calls.append(json.dumps(kwargs.get("messages", []), default=lambda o: vars(o)))
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


def _propose(house, subsystem, target, action, args=None):
    return tool_use("propose_command", {"house_id": house, "subsystem": subsystem,
                                        "target": target, "action": action, "args": args or {}})


def test_l1_executes_and_memory_persists_across_turns(world):
    client = MockClient([
        Resp([_propose("house_a", "light", "living_room", "turn_on")]),
        Resp([text("Living room light is on.")], stop_reason="end_turn"),
        Resp([text("As I said, the living-room light.")], stop_reason="end_turn"),
    ])
    s = ChatSession(world, client=client)
    out1 = s.ask("turn on the living room light")
    assert out1["actions"][0]["status"] == "executed"
    assert world.state.get_state("house_a.light.living_room") == "on"
    out2 = s.ask("which light did you just turn on?")
    # the second model call sees the first turn verbatim — that IS the back-and-forth
    assert "turn on the living room light" in client.messages.calls[-1]
    assert out2["mode"] == "ai"


def test_confirm_dance_executes_as_human_and_token_never_reaches_model(world):
    issued = []
    orig = world.engine.issue_token
    world.engine.issue_token = lambda *a, **k: (issued.append(orig(*a, **k)) or issued[-1])

    client = MockClient([
        Resp([_propose("house_a", "lock", "front_door", "unlock")]),
        Resp([text("Unlocking needs your confirmation — say confirm.")], stop_reason="end_turn"),
    ])
    s = ChatSession(world, client=client)
    out = s.ask("unlock the front door")
    assert out["actions"][0]["status"] == "confirm_required"
    assert len(s.pending) == 1 and "lock.front_door unlock" in out["pending"][0]
    assert world.state.get_state("house_a.lock.front_door") == "locked"   # nothing moved yet

    r = s.confirm(0)                                   # the RESIDENT confirms, not the model
    assert r["status"] == "executed", r
    assert world.state.get_state("house_a.lock.front_door") == "unlocked"
    assert s.pending == []
    assert issued, "human path should have been issued a token"
    for tok in issued:                                  # the invariant, checked literally
        for call in client.messages.calls:
            assert tok not in call, "confirmation token leaked into model context"
    # both steps audited: the AI's gated proposal and the human's execution
    ops = [(rec.operator, rec.status) for rec in world.audit.records
           if rec.target == "front_door" and rec.action == "unlock"]
    assert ("ai", "confirm_required") in ops and ("owner", "executed") in ops


def test_deny_clears_pending_and_informs_next_turn(world):
    client = MockClient([
        Resp([_propose("house_a", "alarm", "panel", "disarm")]),
        Resp([text("Disarm awaits your confirmation.")], stop_reason="end_turn"),
        Resp([text("Understood — leaving it armed.")], stop_reason="end_turn"),
    ])
    s = ChatSession(world, client=client)
    s.ask("disarm the alarm")
    assert len(s.pending) == 1
    r = s.deny(0)
    assert r["status"] == "denied" and s.pending == []
    s.ask("ok")
    assert "resident DENIED" in client.messages.calls[-1]   # the model is told, next turn


def test_offline_and_ai_hold_degrade_to_fallback(world):
    s = ChatSession(world, client=None)
    out = s.ask("arm night")
    assert out["mode"] == "fallback"
    client = MockClient([])                              # would crash if consulted
    world.houses["house_a"].ai_hold = True
    s2 = ChatSession(world, client=client)
    assert s2.ask("turn on the lights")["mode"] == "fallback"
    world.houses["house_a"].ai_hold = False


def test_history_trims_whole_turns_only(world):
    script = []
    for _ in range(8):
        script.append(Resp([text("ok")], stop_reason="end_turn"))
    s = ChatSession(world, client=MockClient(script), max_history_turns=3)
    for i in range(8):
        s.ask(f"turn {i}")
    assert len(s._turn_starts) <= 3
    assert s.messages[0]["role"] == "user"               # never begins mid-pair
    assert "turn 7" in json.dumps(s.messages, default=lambda o: vars(o))


def test_human_operator_required(world):
    import pytest
    with pytest.raises(AssertionError):
        ChatSession(world, operator=Operator(kind="ai", active_house="house_a", name="sneaky"))
