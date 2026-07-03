"""Part 15 — delegation certificates: bounded standing consent.

Converts the per-act vigilance tax of L2 confirmations into a one-time, revocable, budgeted
grant. Invariants under test: trust accrues to the deterministic policy, never the model;
tokens still never enter the model's context; Part 14 envelopes outrank standing consent;
L3+ is not delegable; non-executed outcomes fall back to the pending path."""
import json
from datetime import date, datetime

import pytest

from homeops import build_world
from homeops.ai.session import ChatSession
from homeops.delegations import Delegation, DelegationRegistry, try_delegated_execute
from homeops.permissions import Intent, Operator

OWNER = Operator("owner", "house_a", name="colton")


def at(hour, day=15):
    return lambda: datetime(2026, 1, day, hour, 30)


def lock_intent(target="front_door"):
    return Intent("house_a", "lock", target, "lock")


def night_registry(hour=23, **kw):
    reg = DelegationRegistry(clock=at(hour))
    reg.grant(Delegation(id="d-nightlock", grantor="colton", house_id="house_a",
                         subsystem="lock", action="lock", window=(21, 2), **kw), OWNER)
    return reg


# ---- policy semantics -----------------------------------------------------------------

def test_l3_and_unknown_actions_are_not_delegable():
    reg = DelegationRegistry()
    with pytest.raises(ValueError):
        reg.grant(Delegation(id="x", grantor="c", house_id="house_a",
                             subsystem="generator", action="start"), OWNER)        # L3: per-act only
    with pytest.raises(ValueError):
        reg.grant(Delegation(id="y", grantor="c", house_id="house_a",
                             subsystem="frobnicator", action="engage"), OWNER)     # unknown: fail-closed


def test_window_wraps_midnight():
    reg = night_registry(hour=23)
    assert reg.match(lock_intent()) is not None
    reg.clock = at(1)
    assert reg.match(lock_intent()) is not None
    reg.clock = at(12)
    assert reg.match(lock_intent()) is None


def test_budget_enforced_and_rolls_daily():
    w = build_world(register_automations=False)
    reg = night_registry(hour=23, budget_per_day=2)
    for _ in range(2):
        res, _ = try_delegated_execute(w, lock_intent(), reg)
        assert res is not None and res.ok
    assert try_delegated_execute(w, lock_intent(), reg)[0] is None   # exhausted -> pending path
    reg.clock = at(23, day=16)                                       # next day: budget rolls
    res, _ = try_delegated_execute(w, lock_intent(), reg)
    assert res is not None and res.ok


def test_revocation_and_expiry():
    w = build_world(register_automations=False)
    expired = night_registry(hour=23, expires=date(2026, 1, 10))     # clock says Jan 15
    assert try_delegated_execute(w, lock_intent(), expired)[0] is None
    revoked = night_registry(hour=23)
    revoked.revoke("d-nightlock")
    assert try_delegated_execute(w, lock_intent(), revoked)[0] is None


def test_args_envelope_on_the_delegation_itself():
    w = build_world(register_automations=False)
    w.router.clock = at(18)
    reg = DelegationRegistry(clock=at(18))
    reg.grant(Delegation(id="d-eve-temp", grantor="colton", house_id="house_a",
                         subsystem="climate", action="set_temperature",
                         window=(17, 22), args_within={"temperature": (66, 72)}), OWNER)
    ok = Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 68})
    res, d = try_delegated_execute(w, ok, reg)
    assert res is not None and res.ok and d.id == "d-eve-temp"
    hot = Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 74})
    assert try_delegated_execute(w, hot, reg)[0] is None


def test_semantic_invariants_outrank_standing_consent():
    # A maximally permissive delegation still cannot standing-approve 45°F (Part 14).
    w = build_world(register_automations=False)
    w.router.clock = at(18)
    reg = DelegationRegistry(clock=at(18))
    reg.grant(Delegation(id="d-any-temp", grantor="colton", house_id="house_a",
                         subsystem="climate", action="set_temperature"), OWNER)
    cold = Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 45})
    assert try_delegated_execute(w, cold, reg)[0] is None


def test_delegated_execution_is_audited_with_certificate_id():
    w = build_world(register_automations=False)
    res, _ = try_delegated_execute(w, lock_intent(), night_registry(hour=23))
    assert res.ok
    recs = [r for r in w.router.audit.records if r.status == "delegated"]
    assert recs and recs[-1].target == "d-nightlock"
    ok, bad = w.router.audit.verify_chain()
    assert ok and bad == -1                      # the advisory record joins the hash chain


def test_serialization_round_trip():
    d = Delegation(id="d1", grantor="colton", house_id="house_a", subsystem="lock",
                   action="lock", window=(21, 2), args_within=None,
                   budget_per_day=3, expires=date(2026, 12, 31))
    assert Delegation.from_dict(d.to_dict()) == d


# ---- ChatSession integration: the moat invariant, preserved ----------------------------

class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content, self.stop_reason = content, stop_reason


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


def _propose_lock():
    return Blk(type="tool_use", id="t1", name="propose_command",
               input={"house_id": "house_a", "subsystem": "lock",
                      "target": "front_door", "action": "lock", "args": {}})


def test_session_executes_under_delegation_and_token_never_enters_model_context():
    w = build_world(register_automations=False)
    reg = night_registry(hour=23)

    issued = []                                   # spy on every token the engine mints
    orig = w.router.engine.issue_token

    def spy(intent, operator, ttl=5):
        t = orig(intent, operator, ttl)
        issued.append(t)
        return t

    w.router.engine.issue_token = spy

    client = MockClient([
        Resp([_propose_lock()]),
        Resp([Blk(type="text", text="Locked, per your standing night rule.")],
             stop_reason="end_turn"),
    ])
    s = ChatSession(w, client=client, delegations=reg)
    out = s.ask("we're heading to bed")

    act = out["actions"][0]
    assert act["status"] == "executed" and act.get("delegation") == "d-nightlock"
    assert w.state.get_state("house_a.lock.front_door") == "locked"
    assert out["pending"] == []                   # nothing left for the resident to babysit
    assert issued, "the delegation path must mint a real engine token"
    for call in client.messages.calls:            # every message list the model ever saw
        for tok in issued:
            assert tok not in call                # ...contains no token. The moat holds.


def test_session_without_matching_delegation_still_pends():
    w = build_world(register_automations=False)
    reg = night_registry(hour=12)                 # noon: outside the window
    client = MockClient([
        Resp([_propose_lock()]),
        Resp([Blk(type="text", text="That needs your confirmation.")], stop_reason="end_turn"),
    ])
    s = ChatSession(w, client=client, delegations=reg)
    out = s.ask("lock the front door")
    assert out["actions"][0]["status"] == "confirm_required"
    assert len(s.pending) == 1                    # ordinary Part 10 dance, untouched
    assert s.confirm(0)["status"] == "executed"
