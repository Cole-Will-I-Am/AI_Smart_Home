"""The AI ops layer: a provider-agnostic tool-use loop that PROPOSES engine-gated commands.

Works with Claude (native) or GPT via homeops.ai.providers; the loop itself stores a neutral
transcript and never touches a vendor wire format. Whatever the model, it proposes via dedicated
tools and the permission engine executes/refuses. When the API/internet is unavailable or the
house is on AI-hold, it degrades to the deterministic fallback — the house is never in the AI's
hands for safety.
"""
from __future__ import annotations
from typing import Any

from ..permissions import Intent, Operator
from .fallback import deterministic_response
from .prompts import SYSTEM_PROMPT, render_snapshot
from .providers import as_provider
from .tools import TOOLS

MODEL = "claude-opus-4-8"   # kept for backward compatibility; providers carry their own defaults


def _block_field(block: Any, field: str, default=None):
    if isinstance(block, dict):
        return block.get(field, default)
    return getattr(block, field, default)


class OpsLayer:
    def __init__(self, world, client: Any = None, model: str | None = None) -> None:
        self.world = world
        self.client = client
        self._explicit_model = model
        self._provider = None

    @property
    def provider(self):
        """Resolved lazily: a client is never inspected unless the AI path is actually taken
        (the offline/AI-hold gates must work even with a broken or bogus client)."""
        if self.client is None:
            return None
        if self._provider is None:
            self._provider = as_provider(self.client)
        return self._provider

    @property
    def model(self) -> str:
        if self._explicit_model:
            return self._explicit_model
        try:
            p = self.provider
        except TypeError:
            return MODEL
        return p.default_model if p else MODEL

    # --- tool execution ------------------------------------------------------
    def _run_tool(self, name: str, args: dict, active_house: str) -> dict:
        w = self.world
        if name == "read_state":
            return {"state": render_snapshot(w, active_house)}
        if name == "list_recent_events":
            evs = w.bus.recent(house_id=args.get("house_id"))
            return {"events": [{"type": e.type, "house": e.house_id, "data": e.data} for e in evs]}
        if name == "recommend":
            r = w.router.recommend(args.get("house_id", active_house), args.get("message", ""),
                                   Operator("ai", active_house, "ai-ops"))
            return {"status": r.status, "message": r.message}
        if name == "propose_command":
            # H2: the model is untrusted by construction (Part 17 admits a fully hostile endpoint),
            # so a malformed tool call must be REFUSED, never crash the loop. Missing required
            # fields and a non-dict `args` (a documented local-model dialect) both used to raise
            # out of run()/ask() and kill the turn.
            missing = [k for k in ("subsystem", "target", "action") if not args.get(k)]
            if missing:
                return {"status": "refused", "level": None,
                        "message": f"malformed propose_command: missing {', '.join(missing)}"}
            raw_args = args.get("args", {})
            if not isinstance(raw_args, dict):
                return {"status": "refused", "level": None,
                        "message": f"malformed propose_command: args must be an object, got {type(raw_args).__name__}"}
            # Note: the AI cannot set confirm_cross_house or confirm_token — a human must confirm
            # cross-house and L2+ actions. Cross-house proposals from the AI return confirm_required.
            intent = Intent(
                house_id=args.get("house_id") or active_house,
                subsystem=str(args["subsystem"]), target=str(args["target"]), action=str(args["action"]),
                args=raw_args,
            )
            r = w.router.execute(intent, Operator("ai", active_house, "ai-ops"))
            return {"status": r.status, "message": r.message, "level": r.level}
        return {"error": f"unknown tool {name}"}

    # --- main loop -----------------------------------------------------------
    def run(self, goal: str, active_house: str, max_turns: int = 6) -> dict:
        w = self.world
        house = w.houses[active_house]
        if self.client is None or not house.wan_up or house.ai_hold:
            return deterministic_response(w, goal, active_house)
        provider = self.provider   # resolved only now, past the offline gates

        transcript: list[dict] = [{"role": "user",
                                   "text": f"{render_snapshot(w, active_house)}\n\nGOAL: {goal}"}]
        actions: list[dict] = []
        final_text = ""

        for _ in range(max_turns):
            comp = provider.complete(model=self.model, system=SYSTEM_PROMPT,
                                          tools=TOOLS, transcript=transcript)
            if comp.stop == "refusal":
                return {"mode": "ai", "final": "request refused by safety classifier",
                        "actions": actions, "refusal": True}
            if comp.text:
                final_text = comp.text
            if not comp.tool_calls:
                break
            transcript.append({"role": "assistant", "text": comp.text,
                               "tool_calls": [{"id": t.id, "name": t.name, "input": t.input}
                                              for t in comp.tool_calls]})
            results = []
            for tc in comp.tool_calls:
                out = self._run_tool(tc.name, tc.input, active_house)
                if tc.name in ("propose_command", "recommend"):
                    actions.append({"tool": tc.name, **out})
                results.append({"id": tc.id, "name": tc.name, "output": out})
            transcript.append({"role": "tools", "results": results})

        return {"mode": "ai", "final": final_text, "actions": actions}
