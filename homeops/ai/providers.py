"""Model providers — the model is a plug; the permission engine is the socket.

The ops layer keeps a NEUTRAL transcript (user / assistant+tool_calls / tools results) and a
Provider translates it to and from a vendor wire format. Swapping the model changes capability,
never authority: every provider faces the same gated tools, the same engine, the same absent
token. Supported: Anthropic (Claude, native), OpenAI (GPT, chat-completions SDK), and — Part 17 —
ANY endpoint speaking the chat-completions wire format (Ollama, vLLM, LM Studio, OpenRouter,
DeepSeek, Groq, ...) over homeops' own stdlib HTTP client. The endpoint is untrusted by
construction: a hostile server can propose, and only propose (tests/test_any_model.py).

Neutral transcript message shapes:
    {"role": "user",      "text": str}
    {"role": "assistant", "text": str, "tool_calls": [{"id","name","input"}]}
    {"role": "tools",     "results": [{"id","name","output": dict}]}
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import ipaddress
import json
import os
import urllib.parse


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"          # end | tool_use | refusal


class Provider(ABC):
    name = "abstract"
    default_model = ""

    @abstractmethod
    def complete(self, *, model: str, system: str, tools: list, transcript: list,
                 max_tokens: int = 2048) -> Completion: ...


class AnthropicProvider(Provider):
    name = "anthropic"
    default_model = "claude-opus-4-8"

    def __init__(self, client) -> None:
        self.client = client

    @staticmethod
    def serialize(transcript: list) -> list[dict]:
        msgs: list[dict] = []
        for m in transcript:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                content: list[dict] = []
                if m.get("text"):
                    content.append({"type": "text", "text": m["text"]})
                for tc in m.get("tool_calls", []):
                    content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"],
                                    "input": tc["input"]})
                msgs.append({"role": "assistant", "content": content})
            elif m["role"] == "tools":
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": str(r["output"])}
                    for r in m["results"]]})
        return msgs

    def complete(self, *, model, system, tools, transcript, max_tokens=2048) -> Completion:
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, thinking={"type": "adaptive"},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools, messages=self.serialize(transcript))
        if getattr(resp, "stop_reason", None) == "refusal":
            return Completion(stop="refusal")
        text, calls = "", []
        for b in list(getattr(resp, "content", []) or []):
            t = _field(b, "type")
            if t == "text":
                text = _field(b, "text", "") or text
            elif t == "tool_use":
                calls.append(ToolCall(_field(b, "id"), _field(b, "name"),
                                      dict(_field(b, "input", {}) or {})))
        return Completion(text=text, tool_calls=calls, stop="tool_use" if calls else "end")


class OpenAIProvider(Provider):
    name = "openai"
    default_model = "gpt-5.1"

    def __init__(self, client) -> None:
        self.client = client

    @staticmethod
    def convert_tools(tools: list) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t["input_schema"]}} for t in tools]

    @staticmethod
    def serialize(system: str, transcript: list) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": system}]
        for m in transcript:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                am: dict = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    am["tool_calls"] = [{"id": tc["id"], "type": "function",
                                         "function": {"name": tc["name"],
                                                      "arguments": json.dumps(tc["input"])}}
                                        for tc in m["tool_calls"]]
                msgs.append(am)
            elif m["role"] == "tools":
                for r in m["results"]:
                    msgs.append({"role": "tool", "tool_call_id": r["id"], "content": str(r["output"])})
        return msgs

    def complete(self, *, model, system, tools, transcript, max_tokens=2048) -> Completion:
        resp = self.client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens,
            tools=self.convert_tools(tools), messages=self.serialize(system, transcript))
        return _completion_from_chat_message(_field(resp.choices[0], "message"))


def _completion_from_chat_message(msg) -> Completion:
    """chat-completions `message` -> neutral Completion. Shared by the SDK path and the raw
    HTTP path; deliberately tolerant of local-server dialects (arguments as a dict rather
    than a JSON string) and fail-inert on garbage (unparseable arguments become {})."""
    if _field(msg, "refusal"):
        return Completion(stop="refusal")
    calls = []
    for tc in (_field(msg, "tool_calls") or []):
        fn = _field(tc, "function")
        raw = _field(fn, "arguments")
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = json.loads(raw or "{}")
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(ToolCall(_field(tc, "id"), _field(fn, "name"), args))
    return Completion(text=_field(msg, "content") or "", tool_calls=calls,
                      stop="tool_use" if calls else "end")


class OpenAICompatibleProvider(Provider):
    """Part 17 — the universal plug. Any endpoint speaking the chat-completions wire format,
    reached over homeops' own stdlib HttpClient (no vendor SDK; the core stays stdlib-only).
    An Ollama base_url keeps the ENTIRE loop — model included — on-prem, completing the
    local-first story. Authority is unchanged: same gated tools, same absent token."""
    name = "openai-compatible"
    default_model = ""   # no meaningful universal default: the deployment must name its model

    def __init__(self, base_url: str, api_key: str | None = None, transport=None,
                 timeout: float = 60.0, verify_tls: bool = True,
                 extra_headers: dict | None = None) -> None:
        from ..adapters.http import HttpClient   # local import: keep providers importable alone
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(extra_headers or {})
        self.http = HttpClient(base_url, default_headers=headers,
                               transport=transport, verify_tls=verify_tls, timeout=timeout)

    def complete(self, *, model, system, tools, transcript, max_tokens=2048) -> Completion:
        status, obj = self.http.request("POST", "/chat/completions", json_body={
            "model": model, "max_tokens": max_tokens,
            "tools": OpenAIProvider.convert_tools(tools),
            "messages": OpenAIProvider.serialize(system, transcript),
        })
        if status != 200 or not isinstance(obj, dict):
            raise RuntimeError(f"chat-completions endpoint returned {status}: {str(obj)[:200]}")
        choices = obj.get("choices") or []
        if not choices:
            raise RuntimeError("chat-completions endpoint returned no choices")
        return _completion_from_chat_message(choices[0].get("message") or {})


def _loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def provider_from_config(ai: dict | None, secrets: dict | None = None):
    """Build (provider, model) from a deployment `ai:` section — the BYO-model plug.

    Fail-closed throughout: unknown provider names raise; openai-compatible requires an
    explicit base_url AND model; a non-loopback plaintext endpoint is refused (the estate
    snapshot travels in every request) unless `allow_insecure: true` is set explicitly.
    `provider: none` or an absent section returns (None, None) — the world runs
    deterministic-only, which is always safe."""
    if not ai or str(ai.get("provider", "none")).lower() in ("none", "off", ""):
        return None, None
    name = str(ai["provider"]).lower().replace("_", "-")
    model = ai.get("model")
    env = dict(secrets or {})

    def key(default_env: str) -> str | None:
        var = ai.get("key_env", default_env)
        return env.get(var) or os.environ.get(var)

    if name == "openai-compatible":
        base = ai.get("base_url")
        if not base:
            raise ValueError("ai.provider=openai-compatible requires ai.base_url")
        if not model:
            raise ValueError("ai.provider=openai-compatible requires an explicit ai.model")
        host = urllib.parse.urlparse(base).hostname or ""
        if base.startswith("http://") and not _loopback_host(host) and not ai.get("allow_insecure"):
            raise ValueError(
                f"refusing plaintext non-loopback endpoint {base!r} — the estate snapshot travels "
                "in every request; use https or set ai.allow_insecure: true explicitly")
        p = OpenAICompatibleProvider(base, api_key=key("HOMEOPS_AI_KEY"),
                                     timeout=float(ai.get("timeout", 60.0)),
                                     verify_tls=bool(ai.get("verify_tls", True)))
        return p, model
    if name == "anthropic":
        try:
            import anthropic   # optional SDK; lazy so the stdlib-only core never needs it
        except ImportError as e:
            raise ValueError(
                "the anthropic SDK is not installed — `pip install \"homeops[anthropic]\"` "
                "(or point at any OpenAI-compatible endpoint with --base-url/--ollama, "
                "which needs no SDK)") from e
        return AnthropicProvider(anthropic.Anthropic(api_key=key("ANTHROPIC_API_KEY"))), \
            (model or AnthropicProvider.default_model)
    if name == "openai":
        try:
            import openai      # optional SDK; lazy
        except ImportError as e:
            raise ValueError(
                "the openai SDK is not installed — `pip install \"homeops[openai]\"` "
                "(or point at any OpenAI-compatible endpoint with --base-url/--ollama, "
                "which needs no SDK)") from e
        return OpenAIProvider(openai.OpenAI(api_key=key("OPENAI_API_KEY"))), \
            (model or OpenAIProvider.default_model)
    raise ValueError(f"unknown ai.provider {ai['provider']!r} "
                     "(anthropic | openai | openai-compatible | none)")


def as_provider(client) -> Provider:
    """Accept a Provider, an `anthropic` client, or an `openai` client."""
    if isinstance(client, Provider):
        return client
    chat = getattr(client, "chat", None)
    if chat is not None and hasattr(chat, "completions"):
        return OpenAIProvider(client)
    if hasattr(client, "messages"):
        return AnthropicProvider(client)
    raise TypeError("unrecognized AI client — pass a Provider, an anthropic client, or an openai client")
