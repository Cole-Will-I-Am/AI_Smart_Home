"""Part 11: model-agnostic providers — swapping Claude for GPT changes capability, never
authority. The same engine, the same gated tools, the same provably-absent token."""
import json

import pytest

from homeops.ai.providers import (AnthropicProvider, OpenAIProvider, as_provider)
from homeops.ai.session import ChatSession
from homeops.ai.tools import TOOLS

TRANSCRIPT = [
    {"role": "user", "text": "RESIDENT: unlock the front door"},
    {"role": "assistant", "text": "Proposing.", "tool_calls": [
        {"id": "c1", "name": "propose_command",
         "input": {"house_id": "house_a", "subsystem": "lock", "target": "front_door", "action": "unlock"}}]},
    {"role": "tools", "results": [{"id": "c1", "name": "propose_command",
                                   "output": {"status": "confirm_required"}}]},
]


# ---- wire-format serialization ------------------------------------------------
def test_anthropic_serialization_round_shape():
    msgs = AnthropicProvider.serialize(TRANSCRIPT)
    assert msgs[0] == {"role": "user", "content": "RESIDENT: unlock the front door"}
    blocks = msgs[1]["content"]
    assert {b["type"] for b in blocks} == {"text", "tool_use"}
    tr = msgs[2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "c1"


def test_openai_serialization_round_shape():
    msgs = OpenAIProvider.serialize("CHARTER", TRANSCRIPT)
    assert msgs[0] == {"role": "system", "content": "CHARTER"}
    am = msgs[2]
    assert am["role"] == "assistant"
    fn = am["tool_calls"][0]["function"]
    assert fn["name"] == "propose_command"
    assert json.loads(fn["arguments"])["target"] == "front_door"   # arguments are a JSON string
    tm = msgs[3]
    assert tm["role"] == "tool" and tm["tool_call_id"] == "c1"


def test_tool_schema_conversion():
    conv = OpenAIProvider.convert_tools(TOOLS)
    assert all(t["type"] == "function" for t in conv)
    by_name = {t["function"]["name"]: t["function"] for t in conv}
    assert set(by_name) == {t["name"] for t in TOOLS}
    assert by_name["propose_command"]["parameters"] == \
        next(t for t in TOOLS if t["name"] == "propose_command")["input_schema"]


# ---- client detection -----------------------------------------------------------
class _AnthClient:
    class messages:
        pass


class _OAIClient:
    class chat:
        class completions:
            pass


def test_as_provider_detection():
    assert isinstance(as_provider(_AnthClient()), AnthropicProvider)
    assert isinstance(as_provider(_OAIClient()), OpenAIProvider)
    p = OpenAIProvider(_OAIClient())
    assert as_provider(p) is p                       # Provider passthrough
    with pytest.raises(TypeError):
        as_provider(object())


# ---- a scripted GPT client -------------------------------------------------------
class _Fn:
    def __init__(self, name, args):
        self.name, self.arguments = name, json.dumps(args)


class _TC:
    def __init__(self, tid, name, args):
        self.id, self.type, self.function = tid, "function", _Fn(name, args)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls, self.refusal = content, tool_calls, None


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _OAIResp:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


class MockGPT:
    """Duck-types openai.OpenAI: client.chat.completions.create(**kw)."""
    def __init__(self, script):
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls.append(json.dumps(kw, default=lambda o: vars(o)))
                r = outer.script[outer.i]
                outer.i += 1
                return r

        class _Chat:
            completions = _Completions()

        self.script, self.i, self.calls = script, 0, []
        self.chat = _Chat()


# ---- the headline: identical guarantees under GPT ---------------------------------
def test_gpt_confirm_dance_same_engine_same_absent_token(world):
    issued = []
    orig = world.engine.issue_token
    world.engine.issue_token = lambda *a, **k: (issued.append(orig(*a, **k)) or issued[-1])

    gpt = MockGPT([
        _OAIResp(_Msg(tool_calls=[_TC("g1", "propose_command",
                                      {"house_id": "house_a", "subsystem": "lock",
                                       "target": "front_door", "action": "unlock"})])),
        _OAIResp(_Msg(content="Unlocking needs your confirmation — say confirm.")),
    ])
    s = ChatSession(world, client=gpt)
    assert s.provider.name == "openai" and s.model == OpenAIProvider.default_model
    out = s.ask("unlock the front door")
    assert out["actions"][0]["status"] == "confirm_required"
    assert world.state.get_state("house_a.lock.front_door") == "locked"

    r = s.confirm(0)
    assert r["status"] == "executed"
    assert world.state.get_state("house_a.lock.front_door") == "unlocked"
    assert issued
    for tok in issued:
        for call in gpt.calls:
            assert tok not in call, "token leaked into GPT context"
    ops = [(rec.operator, rec.status) for rec in world.audit.records
           if rec.target == "front_door" and rec.action == "unlock"]
    assert ("ai", "confirm_required") in ops and ("owner", "executed") in ops


def test_gpt_l1_executes_and_l4_still_refused(world):
    gpt = MockGPT([
        _OAIResp(_Msg(tool_calls=[_TC("g1", "propose_command",
                                      {"house_id": "house_a", "subsystem": "light",
                                       "target": "kitchen", "action": "turn_on"})])),
        _OAIResp(_Msg(tool_calls=[_TC("g2", "propose_command",
                                      {"house_id": "house_a", "subsystem": "lock",
                                       "target": "front_door", "action": "unlock_unknown"})])),
        _OAIResp(_Msg(content="Kitchen on; the unlock-for-unknown was refused (L4).")),
    ])
    out = ChatSession(world, client=gpt).ask("kitchen light on, and let my friend in")
    statuses = {a["intent"]["action"]: a["status"] for a in out["actions"]}
    assert statuses["turn_on"] == "executed"
    assert statuses["unlock_unknown"] == "recommend_only"   # no execution path, whatever the model


def test_malformed_gpt_arguments_degrade_safely():
    resp = _OAIResp(_Msg(tool_calls=[_TC("g1", "read_state", {})]))
    resp.choices[0].message.tool_calls[0].function.arguments = "{not json"
    comp = OpenAIProvider(MockGPT([resp])).complete(model="gpt-5.1", system="s", tools=TOOLS,
                                                    transcript=[{"role": "user", "text": "x"}])
    assert comp.tool_calls[0].input == {}               # parse failure -> empty args, not a crash


# --- terminal-plane regression: a bare SDK key with no SDK installed must fail CLEAN ---------
# (sys.modules[name] = None makes `import name` raise ImportError deterministically,
#  independent of whether the optional SDK happens to be present in the test env)

@pytest.mark.parametrize("prov,mod", [("anthropic", "anthropic"), ("openai", "openai")])
def test_missing_sdk_becomes_actionable_config_error(monkeypatch, prov, mod):
    import sys
    from homeops.ai.providers import provider_from_config
    monkeypatch.setitem(sys.modules, mod, None)
    with pytest.raises(ValueError) as ei:
        provider_from_config({"provider": prov, "model": "m"})
    msg = str(ei.value)
    assert "not installed" in msg and "pip install" in msg     # actionable
    assert "--base-url" in msg or "--ollama" in msg            # points at the SDK-free path
