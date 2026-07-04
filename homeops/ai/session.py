"""Resident-facing chat session — stateful back-and-forth with the ops layer.

`ChatSession.ask()` keeps conversation history across turns (Claude remembers "make it warmer"
after "set the bedroom to 68"), refreshes the volatile estate snapshot every turn, and runs the
same gated tool loop as OpsLayer. The critical design point is the confirmation dance:

  1. The AI proposes an L2+ action -> the engine answers `confirm_required` and (because the
     operator kind is "ai") issues NO token. The session records a PendingConfirmation.
  2. The resident says "confirm" -> `confirm()` re-issues the SAME intent as the HUMAN operator,
     receives a token bound to (full intent + human identity), and immediately executes with it.

Both steps are audited under the identity that performed them. Confirmation therefore flows
resident -> engine and never through the model: no token ever enters the AI's context, not as a
tool result, not as text. Offline / AI-hold, the session degrades to the deterministic fallback
exactly like OpsLayer — pending confirmations remain resident-actionable because `confirm()`
never involves the model at all.

Part 15: an optional DelegationRegistry adds a third path. When the AI's proposal comes back
`confirm_required` and a standing certificate covers it, `try_delegated_execute` performs the
token dance ENGINE-SIDE under the grantor's identity, and the model simply sees an executed
tool result naming the certificate — still no token in its context. Everything a delegation
does not cover falls through to the ordinary pending path above; the deterministic-fallback
path is unchanged (its pendings carry no structured intent to match against).
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ..delegations import DelegationRegistry
from ..permissions import Intent, Operator
from .fallback import deterministic_response
from .ops_layer import OpsLayer
from .prompts import SYSTEM_PROMPT, render_snapshot
from .tools import TOOLS


@dataclass
class PendingConfirmation:
    house_id: str
    subsystem: str
    target: str
    action: str
    args: dict = field(default_factory=dict)
    level: int | None = None
    message: str = ""
    attestation: object = None   # engine-signed ground truth for the UI to render (Part 18b)

    def describe(self) -> str:
        extra = f" {self.args}" if self.args else ""
        return f"{self.house_id}: {self.subsystem}.{self.target} {self.action}{extra} (L{self.level})"

    @property
    def effect(self) -> str:
        """The sentence a UI shows the human — from the ENGINE, never the model."""
        return self.attestation.effect if self.attestation is not None else self.describe()


class ChatSession:
    def __init__(self, world, client=None, model: str | None = None, active_house: str = "house_a",
                 operator: Operator | None = None, max_tool_turns: int = 16,
                 max_history_turns: int = 30, delegations: DelegationRegistry | None = None) -> None:
        self.world = world
        self.ops = OpsLayer(world, client=client, model=model)
        self.client = client
        self.active_house = active_house
        # the HUMAN principal on whose behalf confirmations execute — never kind="ai"
        self.operator = operator or Operator(kind="owner", active_house=active_house, name="resident")
        if self.operator.kind == "ai":   # a raise, not an assert: must survive python -O
            raise ValueError("confirmations must belong to a human operator")
        self._validate_active_house(active_house)
        self.max_tool_turns = max_tool_turns
        self.max_history_turns = max_history_turns
        # R6: default to the world's persistent registry so standing consent survives restart
        self.delegations = delegations if delegations is not None else getattr(world, "delegations", None)
        self.messages: list[dict] = []
        self._turn_starts: list[int] = []      # message index where each ask() began (for whole-turn trimming)
        self.pending: list[PendingConfirmation] = []
        self.ops.delegations = self.delegations
        self.ops.pending_provider = lambda: self.pending
        self._notes: list[str] = []            # human-side outcomes to surface to the model next turn

    @property
    def provider(self):
        return self.ops.provider

    @property
    def model(self) -> str:
        return self.ops.model

    # ---- resident turn ---------------------------------------------------------
    def ask(self, text: str) -> dict:
        house = self.world.houses[self.active_house]
        if self.client is None or not house.wan_up or house.ai_hold:
            out = deterministic_response(self.world, text, self.active_house)
            self._register_pending(out.get("actions", []))
            out["pending"] = [p.describe() for p in self.pending]
            return out

        self._trim_history()
        self._turn_starts.append(len(self.messages))
        notes = ""
        if self._notes:
            notes = "\n".join(f"[resident interface note] {n}" for n in self._notes) + "\n\n"
            self._notes.clear()
        pend = ""
        if self.pending:
            pend = "AWAITING RESIDENT CONFIRMATION:\n" + "\n".join(
                f"  {i + 1}. {p.describe()}" for i, p in enumerate(self.pending)) + "\n\n"
        self.messages.append({"role": "user", "text":
                              f"{render_snapshot(self.world, self.active_house, self.operator)}\n\n{notes}{pend}"
                              f"RESIDENT: {text}"})

        actions: list[dict] = []
        final_text = ""
        turn_start = self._turn_starts[-1]   # M1: rollback point if a provider call fails mid-turn
        try:
            for _ in range(self.max_tool_turns):
                comp = self.provider.complete(model=self.model, system=SYSTEM_PROMPT,
                                              tools=TOOLS, transcript=self.messages)
                if comp.stop == "refusal":
                    return {"mode": "ai", "final": "request refused by safety classifier",
                            "actions": actions, "pending": [p.describe() for p in self.pending], "refusal": True}
                if comp.text:
                    final_text = comp.text
                self.messages.append({"role": "assistant", "text": comp.text,
                                      "tool_calls": [{"id": t.id, "name": t.name, "input": t.input}
                                                     for t in comp.tool_calls]})
                if not comp.tool_calls:
                    break
                results = []
                for tc in comp.tool_calls:
                    out = self.ops._run_tool(tc.name, tc.input, self.active_house, operator=self.operator)
                    out.pop("confirm_token", None)   # belt-and-braces: the engine already issues none to "ai"
                    if tc.name in ("propose_command", "propose_plan", "recommend"):
                        entry = {"tool": tc.name, **out}
                        if tc.name == "propose_command":
                            entry["intent"] = dict(tc.input)
                        actions.append(entry)
                    results.append({"id": tc.id, "name": tc.name, "output": out})
                self.messages.append({"role": "tools", "results": results})
        except Exception as e:                       # noqa: BLE001 — provider/transport failure
            # M1: a raised completion left this ask()'s partial messages in the transcript. Rewind
            # to the start of the turn (dropping the orphaned user message and any half-turn), so
            # the next ask() cannot produce two consecutive user turns — which most chat APIs reject
            # — then degrade to the deterministic, still-audited fallback for this request.
            self.messages = self.messages[:turn_start]
            self._turn_starts.pop()
            out = deterministic_response(self.world, text, self.active_house)
            out["degraded"] = f"AI layer unavailable ({type(e).__name__}); used deterministic fallback"
            self._register_pending(out.get("actions", []))
            out["pending"] = [p.describe() for p in self.pending]
            return out
        self._register_pending(actions)
        return {"mode": "ai", "final": final_text, "actions": actions,
                "pending": [p.describe() for p in self.pending]}

    # ---- pending-confirmation bookkeeping ---------------------------------------
    def _register_pending(self, actions: list[dict]) -> None:
        def add(src: dict, level=None, message="", attestation=None) -> None:
            if not src:
                return
            p = PendingConfirmation(
                house_id=src.get("house_id", self.active_house), subsystem=src["subsystem"],
                target=src["target"], action=src["action"], args=dict(src.get("args") or {}),
                level=level, message=message, attestation=attestation)
            if not any(q.describe() == p.describe() for q in self.pending):
                self.pending.append(p)

        for a in actions:
            if a.get("tool") == "propose_plan":
                for step in a.get("steps", []):
                    if step.get("status") == "confirm_required":
                        add(step.get("intent") or {}, step.get("level"), step.get("message", ""),
                            step.get("attestation"))
                continue
            if a.get("status") != "confirm_required":
                continue
            src = a.get("intent") or {}
            if not src:            # fallback path records cmd strings, not intents — skip those
                continue
            add(src, a.get("level"), a.get("message", ""), a.get("attestation"))

    def confirm(self, index: int = 0) -> dict:
        """Execute a pending action AS THE RESIDENT: same intent, human identity, two-step token."""
        if not (0 <= index < len(self.pending)):
            return {"status": "error", "message": f"no pending confirmation #{index + 1}"}
        p = self.pending.pop(index)
        cross = p.house_id != self.operator.active_house
        intent = Intent(p.house_id, p.subsystem, p.target, p.action, dict(p.args),
                        confirm_cross_house=cross)   # saying "confirm" IS the explicit cross-house consent
        # Ground-truth guard (Part 18b): the pending intent is reconstructed from the SESSION'S
        # own fields, never the model's — but the attestation the human just SAW came back through
        # the model's tool result. If a hostile model forged/edited it, the signature won't verify
        # against the engine's key. A mismatch means the human may have consented to a lie: refuse.
        eng = self.world.router.engine
        if p.attestation is not None:
            att = p.attestation
            if not hasattr(att, "signature"):
                from ..permissions import Attestation
                att = Attestation.from_dict(att) if isinstance(att, dict) else None
            truth = eng.attest(intent, self.operator, eng.level(p.subsystem, p.action))
            if att is None or not eng.verify_attestation(att) or att.statement.get("effect") != truth.effect:
                self._notes.append(f"resident confirm REFUSED — attestation mismatch on {p.describe()}")
                return {"status": "refused",
                        "message": "attestation did not verify — the displayed action may have been "
                                   "tampered by the model; refusing to execute unverified consent"}
        r = self.world.router.execute(intent, self.operator)
        if r.status == "confirm_required" and r.confirm_token:
            intent.confirm_token = r.confirm_token   # token: engine -> human path -> engine; never the model
            r = self.world.router.execute(intent, self.operator)
        self._notes.append(f"resident CONFIRMED {p.describe()} -> {r.status}: {r.message}")
        return {"status": r.status, "message": r.message, "level": r.level,
                "rollback_token": r.rollback_token}

    def deny(self, index: int = 0) -> dict:
        if not (0 <= index < len(self.pending)):
            return {"status": "error", "message": f"no pending confirmation #{index + 1}"}
        p = self.pending.pop(index)
        self._notes.append(f"resident DENIED {p.describe()} — do not retry unless asked")
        return {"status": "denied", "message": f"denied: {p.describe()}"}

    def switch_house(self, house_id: str) -> None:
        self._validate_active_house(house_id)
        self.active_house = house_id
        self.operator.active_house = house_id

    def _validate_active_house(self, house_id: str) -> None:
        if house_id not in self.world.houses:
            raise KeyError(house_id)
        scope = self.operator.houses
        if scope != "*" and house_id not in scope:
            raise PermissionError(f"property {house_id} is out of scope for operator {self.operator.name}")

    # ---- history hygiene ---------------------------------------------------------
    def _trim_history(self) -> None:
        """Drop whole oldest turns (never splitting a tool_use/tool_result pair)."""
        while len(self._turn_starts) >= self.max_history_turns and len(self._turn_starts) > 1:
            cut = self._turn_starts[1]
            self.messages = self.messages[cut:]
            self._turn_starts = [i - cut for i in self._turn_starts[1:]]
