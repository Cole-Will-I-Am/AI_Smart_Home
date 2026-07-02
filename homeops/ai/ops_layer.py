"""The Claude ops layer: a manual tool-use loop that PROPOSES engine-gated commands.

Model `claude-opus-4-8`, adaptive thinking, a frozen cached system prefix, and the volatile
two-house snapshot in the user turn. Claude proposes via dedicated tools; the permission engine
executes/refuses. When the API/internet is unavailable or the house is on AI-hold, it degrades to
the deterministic fallback — the house is never in the AI's hands for safety.
"""
from __future__ import annotations
from typing import Any

from ..permissions import Intent, Operator
from .prompts import SYSTEM_PROMPT, render_snapshot
from .tools import TOOLS
from .fallback import deterministic_response

MODEL = "claude-opus-4-8"


def _block_field(block: Any, field: str, default=None):
    if isinstance(block, dict):
        return block.get(field, default)
    return getattr(block, field, default)


class OpsLayer:
    def __init__(self, world, client: Any = None, model: str = MODEL) -> None:
        self.world = world
        self.client = client
        self.model = model

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
            intent = Intent(
                house_id=args.get("house_id", active_house),
                subsystem=args["subsystem"], target=args["target"], action=args["action"],
                args=args.get("args", {}) or {}, confirm_cross_house=bool(args.get("confirm_cross_house")),
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

        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        user = f"{render_snapshot(w, active_house)}\n\nGOAL: {goal}"
        messages: list[dict] = [{"role": "user", "content": user}]
        actions: list[dict] = []
        final_text = ""

        for _ in range(max_turns):
            resp = self.client.messages.create(
                model=self.model, max_tokens=2048,
                thinking={"type": "adaptive"},
                system=system, tools=TOOLS, messages=messages,
            )
            if getattr(resp, "stop_reason", None) == "refusal":
                return {"mode": "ai", "final": "request refused by safety classifier", "actions": actions,
                        "refusal": True}

            content = list(getattr(resp, "content", []))
            tool_uses = [b for b in content if _block_field(b, "type") == "tool_use"]
            for b in content:
                if _block_field(b, "type") == "text":
                    final_text = _block_field(b, "text", "") or final_text

            if not tool_uses:
                break

            messages.append({"role": "assistant", "content": content})
            results = []
            for tu in tool_uses:
                out = self._run_tool(_block_field(tu, "name"), _block_field(tu, "input", {}) or {}, active_house)
                if _block_field(tu, "name") in ("propose_command", "recommend"):
                    actions.append({"tool": _block_field(tu, "name"), **out})
                results.append({"type": "tool_result", "tool_use_id": _block_field(tu, "id"),
                                "content": str(out)})
            messages.append({"role": "user", "content": results})

        return {"mode": "ai", "final": final_text, "actions": actions}
