"""Part 20 — terminal-first, bring-your-own-model. A developer selects the reasoning layer with a
flag or an env var; the CLI routes it through the Part-17 provider factory. Authority is unchanged
by the choice — this only tests that the *plumbing* (flags/env -> ai: dict -> provider) resolves
correctly and fails closed. The engine's invariance under model substitution is proven elsewhere."""
import pytest

from homeops import build_world
from homeops.cli import resolve_ai_config, build_chat_session, _parse_chat_args
from homeops.ai.providers import OpenAICompatibleProvider, Provider, Completion


# ---- resolution precedence (pure) ------------------------------------------------------
def test_no_config_is_deterministic_fallback():
    assert resolve_ai_config({}, environ={}) == {"provider": "none"}


def test_ollama_shorthand_expands_to_local_endpoint():
    ai = resolve_ai_config({"ollama": "qwen3:14b"}, environ={})
    assert ai == {"provider": "openai-compatible", "model": "qwen3:14b",
                  "base_url": "http://127.0.0.1:11434/v1"}


def test_explicit_base_url_implies_openai_compatible():
    ai = resolve_ai_config({"base_url": "https://openrouter.ai/api/v1", "model": "x"}, environ={})
    assert ai["provider"] == "openai-compatible" and ai["base_url"].endswith("/v1")


def test_flags_beat_env():
    ai = resolve_ai_config({"ollama": "llama3"},
                           environ={"ANTHROPIC_API_KEY": "k", "HOMEOPS_AI_PROVIDER": "openai"})
    assert ai["provider"] == "openai-compatible" and ai["model"] == "llama3"


def test_homeops_env_provider():
    ai = resolve_ai_config({}, environ={"HOMEOPS_AI_BASE_URL": "http://127.0.0.1:1234/v1",
                                        "HOMEOPS_AI_MODEL": "local"})
    assert ai["provider"] == "openai-compatible" and ai["model"] == "local"


def test_sdk_key_autodetect():
    assert resolve_ai_config({}, environ={"ANTHROPIC_API_KEY": "k"})["provider"] == "anthropic"
    assert resolve_ai_config({}, environ={"OPENAI_API_KEY": "k"})["provider"] == "openai"


def test_env_model_applies_to_autodetected_sdk():
    ai = resolve_ai_config({}, environ={"ANTHROPIC_API_KEY": "k", "HOMEOPS_AI_MODEL": "claude-x"})
    assert ai == {"provider": "anthropic", "model": "claude-x"}


# ---- argument parsing -------------------------------------------------------------------
def test_parse_chat_args_positional_and_flags():
    house, args = _parse_chat_args(["house_b", "--ollama", "qwen3:14b"])
    assert house == "house_b" and args["ollama"] == "qwen3:14b"
    house, args = _parse_chat_args(["--model", "gpt-5.1", "--base-url", "https://x/v1"])
    assert house == "house_a" and args["model"] == "gpt-5.1" and args["base_url"] == "https://x/v1"


# ---- session building (no network — providers construct lazily) -------------------------
def test_build_session_deterministic_when_unconfigured():
    w = build_world(register_automations=False)
    session, banner = build_chat_session(w, {}, environ={})
    assert session.provider is None and "deterministic" in banner


def test_build_session_with_ollama_is_openai_compatible():
    w = build_world(register_automations=False)
    session, banner = build_chat_session(w, {"ollama": "qwen3:14b"}, house="house_a")
    assert isinstance(session.provider, OpenAICompatibleProvider)
    assert session.model == "qwen3:14b" and "openai-compatible" in banner


def test_build_session_fails_closed_on_partial_config():
    w = build_world(register_automations=False)
    with pytest.raises(ValueError):   # openai-compatible base_url but no model
        build_chat_session(w, {"base_url": "http://127.0.0.1:11434/v1"})


# ---- a full terminal turn drives the real engine through a fake local model ------------
class FakeLocalModel(Provider):
    """Stands in for a local Ollama/vLLM endpoint: proposes an L1 light, then an L2 unlock."""
    name = "fake-local"
    default_model = "fake"

    def __init__(self):
        self.turn = 0

    def complete(self, *, model, system, tools, transcript, max_tokens=2048):
        from homeops.ai.providers import ToolCall
        self.turn += 1
        if self.turn == 1:
            return Completion(text="Lights on.", stop="tool_use", tool_calls=[
                ToolCall("t1", "propose_command", {"house_id": "house_a", "subsystem": "light",
                                                   "target": "kitchen", "action": "turn_on"})])
        return Completion(text="Unlocking.", stop="tool_use", tool_calls=[
            ToolCall("t2", "propose_command", {"house_id": "house_a", "subsystem": "lock",
                                               "target": "front_door", "action": "unlock"})])


def test_terminal_turn_executes_l1_and_surfaces_attested_l2(monkeypatch):
    w = build_world(register_automations=False)
    from homeops.ai.session import ChatSession
    session = ChatSession(w, client=FakeLocalModel(), active_house="house_a")
    out1 = session.ask("turn on the kitchen light")
    assert any(a["status"] == "executed" for a in out1["actions"])
    assert w.state.get_state("house_a.light.kitchen") == "on"
    out2 = session.ask("unlock the front door")
    assert out2["actions"][0]["status"] == "confirm_required"
    # the pending shows the ENGINE's attested effect, not the model's "Unlocking." prose:
    assert session.pending[0].effect == "[L2] UNLOCK house_a/front_door"
    assert session.confirm(0)["status"] == "executed"
    assert w.state.get_state("house_a.lock.front_door") == "unlocked"
