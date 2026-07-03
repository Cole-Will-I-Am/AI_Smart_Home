"""Model providers — the model is a plug; the permission engine is the socket.

The ops layer keeps a NEUTRAL transcript (user / assistant+tool_calls / tools results) and a
Provider translates it to and from a vendor wire format. Swapping the model changes capability,
never authority: every provider faces the same gated tools, the same engine, the same absent
token. Supported: Anthropic (Claude, native) and OpenAI (GPT, chat-completions tool calling).

Neutral transcript message shapes:
    {"role": "user",      "text": str}
    {"role": "assistant", "text": str, "tool_calls": [{"id","name","input"}]}
    {"role": "tools",     "results": [{"id","name","output": dict}]}
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json


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
        msg = _field(resp.choices[0], "message")
        if _field(msg, "refusal"):
            return Completion(stop="refusal")
        calls = []
        for tc in (_field(msg, "tool_calls") or []):
            fn = _field(tc, "function")
            try:
                args = json.loads(_field(fn, "arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(_field(tc, "id"), _field(fn, "name"), args))
        return Completion(text=_field(msg, "content") or "", tool_calls=calls,
                          stop="tool_use" if calls else "end")


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
