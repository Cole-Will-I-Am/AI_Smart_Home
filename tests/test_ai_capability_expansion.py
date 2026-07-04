"""Capability-forward AI tools: rich reads, per-step plans, and delegated L3 coverage."""
import json
from datetime import date

from homeops.ai.ops_layer import OpsLayer
from homeops.ai.session import ChatSession
from homeops.delegations import Delegation, DelegationRegistry, grant_standing_authority
from homeops.permissions import Intent, Operator


OWNER = Operator("owner", "house_a", "colton")


def _blob(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=lambda o: vars(o))


def test_l0_read_tools_are_correct_scoped_redacted_and_non_mutating(bare):
    setup_token = bare.router.execute(
        Intent("house_a", "lock", "front_door", "unlock"), OWNER).confirm_token
    rb = bare.router.execute(Intent("house_a", "light", "kitchen", "turn_on"), OWNER).rollback_token
    bare.health.mark_offline("house_a.lock.front_door")

    ops = OpsLayer(bare)
    state_after_setup = {e.entity_id: e.state for e in bare.state.all_entities()}
    audit_after_setup = len(bare.audit.records)

    explain = ops._run_tool(
        "explain_action",
        {"house_id": "house_a", "subsystem": "battery", "action": "set_mode"},
        "house_a",
    )
    health = ops._run_tool(
        "device_health",
        {"house_id": "house_a", "entity_id": "house_a.lock.front_door"},
        "house_a",
    )
    pending = ops._run_tool("list_pending_confirmations", {"house_id": "house_a"}, "house_a")
    tail = ops._run_tool("read_audit_tail", {"house_id": "house_a", "n": 10}, "house_a")
    scoped = Operator("owner", "house_a", "scoped", houses={"house_a"})
    situation = ops._run_tool("situation", {}, "house_a", operator=scoped)

    assert explain["level"] == 3
    assert explain["requires_confirmation"] is True
    assert explain["safety_critical"] is False
    assert explain["delegable"] is True
    assert health["devices"][0]["status"] == "offline"
    assert any("unlock" in p["description"] for p in pending["pending"])
    assert [h["house_id"] for h in situation["houses"]] == ["house_a"]

    visible = _blob({"pending": pending, "tail": tail, "situation": situation})
    assert setup_token not in visible
    assert rb not in visible
    assert "confirm_token" not in visible
    assert len(bare.audit.records) == audit_after_setup
    assert {e.entity_id: e.state for e in bare.state.all_entities()} == state_after_setup


def test_situation_refuses_out_of_scope_house_without_leaking_data(bare):
    ops = OpsLayer(bare)
    scoped = Operator("owner", "house_a", "scoped", houses={"house_a"})
    out = ops._run_tool("situation", {"house_id": "house_b"}, "house_a", operator=scoped)
    assert out["houses"] == []
    assert "out of scope" in out["message"]


def test_propose_plan_gates_each_step_independently_without_batch_execution(bare):
    out = OpsLayer(bare)._run_tool(
        "propose_plan",
        {"house_id": "house_a", "steps": [
            {"subsystem": "light", "target": "kitchen", "action": "turn_on"},
            {"subsystem": "lock", "target": "front_door", "action": "unlock"},
            {"subsystem": "battery", "target": "main", "action": "set_mode",
             "args": {"mode": "backup"}},
        ]},
        "house_a",
    )

    assert [s["status"] for s in out["steps"]] == [
        "executed", "confirm_required", "confirm_required",
    ]
    assert bare.state.get_state("house_a.light.kitchen") == "on"
    assert bare.state.get_state("house_a.lock.front_door") == "locked"
    assert bare.state.get_state("house_a.battery.main") == "grid"
    assert "confirm_token" not in _blob(out)


def test_propose_plan_false_when_skips_without_audit_or_mutation(bare):
    before = len(bare.audit.records)
    out = OpsLayer(bare)._run_tool(
        "propose_plan",
        {"house_id": "house_a", "steps": [
            {"subsystem": "light", "target": "kitchen", "action": "turn_on",
             "when": {"entity_id": "house_a.light.kitchen", "equals": "on"}},
        ]},
        "house_a",
    )
    assert out["steps"][0]["status"] == "skipped"
    assert bare.state.get_state("house_a.light.kitchen") == "off"
    assert len(bare.audit.records) == before


def test_propose_plan_delegated_l3_executes_and_token_stays_hidden(bare):
    bare.delegations.grant(
        Delegation("d-battery", "colton", "house_a", "battery", "set_mode"),
        OWNER,
    )
    issued = []
    orig = bare.engine.issue_token

    def spy(intent, operator, ttl=5):
        tok = orig(intent, operator, ttl)
        issued.append(tok)
        return tok

    bare.engine.issue_token = spy
    out = OpsLayer(bare)._run_tool(
        "propose_plan",
        {"house_id": "house_a", "steps": [
            {"subsystem": "battery", "target": "main", "action": "set_mode",
             "args": {"mode": "backup"}},
        ]},
        "house_a",
    )

    step = out["steps"][0]
    assert step["status"] == "executed"
    assert step["delegation"] == "d-battery"
    assert bare.state.get_state("house_a.battery.main") == "backup"
    assert issued
    assert all(tok not in _blob(out) for tok in issued)


def test_standing_authority_does_not_delegate_safety_critical_l3_plan_step(bare):
    reg = bare.delegations
    grant_standing_authority(
        OWNER, "house_a", max_level=3, window=(0, 23), budget=2,
        expiry=date(2026, 12, 31), registry=reg)
    out = OpsLayer(bare)._run_tool(
        "propose_plan",
        {"house_id": "house_a", "steps": [
            {"subsystem": "generator", "target": "main", "action": "start"},
        ]},
        "house_a",
    )
    assert out["steps"][0]["status"] == "confirm_required"
    assert "delegation" not in out["steps"][0]
    assert bare.state.get_state("house_a.generator.main") == "off"


def test_propose_plan_cross_house_step_requires_confirmation_and_does_not_execute(bare):
    out = OpsLayer(bare)._run_tool(
        "propose_plan",
        {"house_id": "house_a", "steps": [
            {"house_id": "house_b", "subsystem": "light", "target": "kitchen", "action": "turn_on"},
        ]},
        "house_a",
    )
    assert out["steps"][0]["status"] == "confirm_required"
    assert "cross-house" in out["steps"][0]["message"]
    assert bare.state.get_state("house_b.light.kitchen") == "off"


def test_propose_plan_l4_l5_remain_non_executable(bare):
    out = OpsLayer(bare)._run_tool(
        "propose_plan",
        {"house_id": "house_a", "steps": [
            {"subsystem": "lock", "target": "front_door", "action": "unlock_unknown"},
            {"subsystem": "safety", "target": "panel", "action": "bypass"},
        ]},
        "house_a",
    )
    assert [s["status"] for s in out["steps"]] == ["recommend_only", "prohibited"]


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


def test_chat_session_registers_plan_pending_and_tokens_never_reach_model(bare):
    issued = []
    orig = bare.engine.issue_token
    bare.engine.issue_token = lambda *a, **k: (issued.append(orig(*a, **k)) or issued[-1])

    client = MockClient([
        Resp([Blk(type="tool_use", id="p1", name="propose_plan", input={
            "house_id": "house_a",
            "steps": [
                {"subsystem": "lock", "target": "front_door", "action": "unlock"},
            ],
        })]),
        Resp([Blk(type="text", text="Unlocking awaits confirmation.")], stop_reason="end_turn"),
    ])
    s = ChatSession(bare, client=client)
    out = s.ask("unlock the front door as a plan")
    assert out["actions"][0]["steps"][0]["status"] == "confirm_required"
    assert len(s.pending) == 1

    confirmed = s.confirm(0)
    assert confirmed["status"] == "executed"
    assert bare.state.get_state("house_a.lock.front_door") == "unlocked"
    assert issued
    for call in client.messages.calls:
        for tok in issued:
            assert tok not in call
