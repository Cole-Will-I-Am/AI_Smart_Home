"""The AI ops layer: a provider-agnostic tool-use loop that PROPOSES engine-gated commands.

Works with Claude (native) or GPT via homeops.ai.providers; the loop itself stores a neutral
transcript and never touches a vendor wire format. Whatever the model, it proposes via dedicated
tools and the permission engine executes/refuses. When the API/internet is unavailable or the
house is on AI-hold, it degrades to the deterministic fallback — the house is never in the AI's
hands for safety.
"""
from __future__ import annotations
from dataclasses import asdict
import re
from typing import Any

from ..delegations import is_delegable_action, try_delegated_execute
from ..permissions import (
    DESTRUCTIVE_COOLDOWN, SAFETY_CRITICAL, Intent, Operator, requires_confirmation,
)
from .fallback import deterministic_response
from .prompts import SYSTEM_PROMPT, render_snapshot
from .providers import as_provider
from .tools import TOOLS

MODEL = "claude-opus-4-8"   # kept for backward compatibility; providers carry their own defaults


def _block_field(block: Any, field: str, default=None):
    if isinstance(block, dict):
        return block.get(field, default)
    return getattr(block, field, default)


def _model_operator(active_house: str) -> Operator:
    return Operator("ai", active_house, "ai-ops")


def _scope_allows(operator: Operator | None, house_id: str) -> bool:
    scope = getattr(operator, "houses", "*")
    return scope == "*" or house_id in scope


def _visible_houses(world, house_id: str | None, operator: Operator | None) -> tuple[list[str], str | None]:
    if house_id:
        if house_id not in world.houses:
            return [], f"unknown house {house_id!r}"
        if not _scope_allows(operator, house_id):
            return [], f"property {house_id} is out of scope"
        return [house_id], None
    return [hid for hid in world.houses if _scope_allows(operator, hid)], None


def _entity_id_for(house_id: str, raw: str) -> str:
    raw = str(raw)
    if raw.startswith("house_"):
        return raw
    if raw.count(".") == 1:
        return f"{house_id}.{raw}"
    return raw


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            low = str(k).lower()
            if "token" in low or "secret" in low or "password" in low or low in {"key", "api_key"}:
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return [_redact(v) for v in value]
    return value


def _literal(text: str):
    s = text.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def _same_value(actual, expected) -> bool:
    return actual == expected or str(actual) == str(expected)


class OpsLayer:
    def __init__(self, world, client: Any = None, model: str | None = None) -> None:
        self.world = world
        self.client = client
        self._explicit_model = model
        self._provider = None
        self.delegations = getattr(world, "delegations", None)
        self.pending_provider = None

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

    # --- read helpers --------------------------------------------------------
    def _pending_confirmations(self, active_house: str, house_id: str | None,
                               operator: Operator | None) -> dict:
        houses, err = _visible_houses(self.world, house_id, operator)
        if err:
            return {"pending": [], "message": err}
        allowed = set(houses)
        pending = []
        if self.pending_provider is not None:
            for idx, p in enumerate(list(self.pending_provider())):
                if p.house_id in allowed:
                    pending.append({
                        "index": idx, "house_id": p.house_id, "level": p.level,
                        "description": p.describe(), "effect": p.effect,
                    })
            return {"pending": pending}

        open_by_key: dict[tuple, dict] = {}
        for rec in self.world.audit.records:
            if rec.house_id not in allowed:
                continue
            key = (rec.house_id, rec.subsystem, rec.target, rec.action,
                   tuple(sorted((rec.args or {}).items())))
            if rec.status == "confirm_required":
                open_by_key[key] = {
                    "house_id": rec.house_id, "level": rec.level,
                    "description": f"{rec.house_id}: {rec.subsystem}.{rec.target} "
                                   f"{rec.action} {rec.args or {}} (L{rec.level})",
                    "message": rec.message,
                }
            elif rec.status in {"executed", "refused", "prohibited", "recommend_only", "unverified"}:
                open_by_key.pop(key, None)
        return {"pending": list(open_by_key.values())}

    def _audit_tail(self, house_id: str | None, n: int, operator: Operator | None) -> dict:
        houses, err = _visible_houses(self.world, house_id, operator)
        if err:
            return {"records": [], "message": err}
        allowed = set(houses)
        try:
            count = max(1, min(500, int(n)))
        except (TypeError, ValueError):
            count = 20
        recs = [r for r in self.world.audit.records if r.house_id in allowed][-count:]
        return {"records": [_redact(asdict(r)) for r in recs]}

    def _device_health(self, house_id: str | None, entity_id: str | None,
                       operator: Operator | None) -> dict:
        if entity_id:
            hid = entity_id.split(".", 1)[0] if str(entity_id).startswith("house_") else (house_id or "")
            if not hid:
                return {"devices": [], "message": "entity_id without house requires house_id"}
            houses, err = _visible_houses(self.world, hid, operator)
            if err:
                return {"devices": [], "message": err}
            eids = [_entity_id_for(houses[0], entity_id)]
        else:
            houses, err = _visible_houses(self.world, house_id, operator)
            if err:
                return {"devices": [], "message": err}
            eids = [e.entity_id for hid in houses for e in self.world.state.all_entities(hid)]
        devices = []
        for eid in eids:
            ent = self.world.state.entity(eid)
            status = "unknown"
            if ent is not None and self.world.health is not None:
                status = self.world.health.status(eid, self.world.engine.tick)
            devices.append({"entity_id": eid, "status": status,
                            "state": ent.state if ent is not None else None})
        return {"devices": devices}

    def _situation(self, house_id: str | None, operator: Operator | None) -> dict:
        houses, err = _visible_houses(self.world, house_id, operator)
        if err:
            return {"houses": [], "message": err}
        out = []
        for hid in houses:
            house = self.world.houses[hid]
            statuses = {"ok": 0, "stale": 0, "offline": 0, "unknown": 0}
            if self.world.health is not None:
                for ent in self.world.state.all_entities(hid):
                    statuses[self.world.health.status(ent.entity_id, self.world.engine.tick)] += 1
            key_states = {}
            for eid in (
                f"{hid}.alarm.panel", f"{hid}.water.main_valve", f"{hid}.power.panel",
                f"{hid}.battery.main", f"{hid}.generator.main", f"{hid}.hvac.main",
            ):
                key_states[eid] = self.world.state.get_state(eid)
            locks = {e.name: e.state for e in self.world.state.all_entities(hid) if e.subsystem == "lock"}
            pending = self._pending_confirmations(hid, hid, operator)["pending"]
            out.append({
                "house_id": hid, "alias": house.alias, "mode": house.mode,
                "wan_up": house.wan_up, "grid_up": house.grid_up, "ai_hold": house.ai_hold,
                "health": statuses, "locks": locks, "key_states": key_states,
                "pending_confirmations": len(pending),
            })
        return {"houses": out}

    def _explain_action(self, active_house: str, args: dict) -> dict:
        subsystem, action = args.get("subsystem"), args.get("action")
        if not subsystem or not action:
            return {"level": None, "requires_confirmation": False, "safety_critical": False,
                    "delegable": False, "reason": "subsystem and action are required"}
        house_id = args.get("house_id") or active_house
        level = self.world.router.engine.level(str(subsystem), str(action))
        key = (str(subsystem), str(action))
        safety = key in SAFETY_CRITICAL
        delegable = is_delegable_action(*key)
        if level is None:
            return {"house_id": house_id, "level": None, "requires_confirmation": False,
                    "safety_critical": safety, "delegable": False,
                    "reason": f"unknown action {subsystem}.{action}"}
        if level == 5:
            reason = "L5 prohibited — no execution path"
        elif level == 4:
            reason = "L4 recommend-only — use recommend(), not actuation"
        elif safety:
            reason = "safety-critical; router requires health/read-back gates"
        elif key in DESTRUCTIVE_COOLDOWN:
            reason = "destructive/cooldown-gated; per-act confirmation remains required"
        elif level >= 3:
            reason = "L3 power/infra; approved hardware and human confirmation required"
        elif level >= 2:
            reason = "L2 security/utility; AI proposals require human confirmation"
        else:
            reason = "L1 routine action; router may execute if all gates pass"
        needs = False
        if level in (1, 2, 3):
            needs = requires_confirmation(
                Intent(house_id, str(subsystem), "_", str(action)), _model_operator(active_house), level,
            ) or level >= 2
        return {"house_id": house_id, "level": level, "requires_confirmation": needs,
                "safety_critical": safety, "delegable": delegable, "reason": reason}

    # --- plan helpers --------------------------------------------------------
    def _eval_when(self, house_id: str, when) -> tuple[bool, str | None]:
        if when in (None, "", {}):
            return True, None
        if isinstance(when, str):
            m = re.match(r"^\s*([A-Za-z0-9_.:-]+)\s*(==|!=)\s*(.+?)\s*$", when)
            if not m:
                return False, "invalid when predicate"
            eid, op, raw = m.groups()
            actual = self.world.state.get_state(_entity_id_for(house_id, eid))
            expected = _literal(raw)
            ok = _same_value(actual, expected)
            return (ok if op == "==" else not ok), f"{_entity_id_for(house_id, eid)} {op} {expected!r}"
        if not isinstance(when, dict):
            return False, "invalid when predicate"
        raw_eid = when.get("entity_id") or when.get("entity") or when.get("target")
        if not raw_eid:
            return False, "when predicate requires entity_id"
        eid = _entity_id_for(house_id, raw_eid)
        actual = self.world.state.get_state(eid)
        if "equals" in when or "eq" in when or "state" in when:
            expected = when.get("equals", when.get("eq", when.get("state")))
            return _same_value(actual, expected), f"{eid} == {expected!r}"
        if "not_equals" in when or "ne" in when:
            expected = when.get("not_equals", when.get("ne"))
            return not _same_value(actual, expected), f"{eid} != {expected!r}"
        if "in" in when:
            vals = when["in"]
            if not isinstance(vals, list):
                return False, "when 'in' must be a list"
            return any(_same_value(actual, v) for v in vals), f"{eid} in {vals!r}"
        if "not_in" in when:
            vals = when["not_in"]
            if not isinstance(vals, list):
                return False, "when 'not_in' must be a list"
            return not any(_same_value(actual, v) for v in vals), f"{eid} not_in {vals!r}"
        return False, "unsupported when predicate"

    def _run_model_intent(self, intent: Intent, active_house: str) -> dict:
        r = self.world.router.execute(intent, _model_operator(active_house))
        out = {"status": r.status, "message": r.message, "level": r.level}
        if r.attestation is not None:
            out["attestation"] = r.attestation
        if r.status == "confirm_required" and self.delegations is not None:
            d_res, d = try_delegated_execute(self.world, intent, self.delegations)
            if d_res is not None:
                out = {"status": d_res.status, "message": d_res.message,
                       "level": d_res.level, "delegation": d.id}
        return _redact(out)

    def _intent_from_step(self, default_house: str, step: dict) -> tuple[Intent | None, dict | None]:
        missing = [k for k in ("subsystem", "target", "action") if not step.get(k)]
        if missing:
            return None, {"status": "refused", "level": None,
                          "message": f"malformed plan step: missing {', '.join(missing)}"}
        raw_args = step.get("args", {})
        if not isinstance(raw_args, dict):
            return None, {"status": "refused", "level": None,
                          "message": f"malformed plan step: args must be an object, got {type(raw_args).__name__}"}
        return Intent(
            house_id=step.get("house_id") or default_house,
            subsystem=str(step["subsystem"]), target=str(step["target"]), action=str(step["action"]),
            args=dict(raw_args),
        ), None

    def _propose_plan(self, args: dict, active_house: str) -> dict:
        steps = args.get("steps")
        if not isinstance(steps, list):
            return {"status": "refused", "message": "propose_plan requires steps as a list", "steps": []}
        default_house = args.get("house_id") or active_house
        results = []
        for idx, raw in enumerate(steps):
            if not isinstance(raw, dict):
                results.append({"index": idx, "status": "refused", "message": "plan step must be an object"})
                continue
            intent, err = self._intent_from_step(default_house, raw)
            if err is not None:
                err["index"] = idx
                results.append(err)
                continue
            ok, why = self._eval_when(intent.house_id, raw.get("when"))
            intent_dict = {
                "house_id": intent.house_id, "subsystem": intent.subsystem, "target": intent.target,
                "action": intent.action, "args": dict(intent.args),
            }
            if not ok:
                results.append({"index": idx, "status": "skipped", "message": why or "when predicate false",
                                "intent": intent_dict})
                continue
            out = self._run_model_intent(intent, active_house)
            results.append({"index": idx, **out, "intent": intent_dict})
        return {"status": "plan_evaluated", "steps": results}

    # --- tool execution ------------------------------------------------------
    def _run_tool(self, name: str, args: dict, active_house: str, operator: Operator | None = None) -> dict:
        w = self.world
        if name == "read_state":
            return {"state": render_snapshot(w, active_house, operator)}
        if name == "list_recent_events":
            house_id = args.get("house_id")
            houses, err = _visible_houses(w, house_id, operator)
            if err:
                return {"events": [], "message": err}
            evs = [e for e in w.bus.recent(house_id=house_id) if e.house_id in set(houses)]
            return {"events": [{"type": e.type, "house": e.house_id, "data": e.data} for e in evs]}
        if name == "explain_action":
            return self._explain_action(active_house, args)
        if name == "device_health":
            return self._device_health(args.get("house_id"), args.get("entity_id"), operator)
        if name == "list_pending_confirmations":
            return self._pending_confirmations(active_house, args.get("house_id"), operator)
        if name == "read_audit_tail":
            return self._audit_tail(args.get("house_id"), args.get("n", 20), operator)
        if name == "situation":
            return self._situation(args.get("house_id"), operator)
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
            # The signed attestation is engine ground truth for the human UI. It is NOT secret
            # (unlike the token) — it CANNOT authorize anything, only describe — so returning it
            # to the model's tool result is safe, and the session lifts it onto the pending item.
            return self._run_model_intent(intent, active_house)
        if name == "propose_plan":
            return self._propose_plan(args, active_house)
        return {"error": f"unknown tool {name}"}

    # --- main loop -----------------------------------------------------------
    def run(self, goal: str, active_house: str, max_turns: int = 16) -> dict:
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
                if tc.name in ("propose_command", "propose_plan", "recommend"):
                    actions.append({"tool": tc.name, **out})
                results.append({"id": tc.id, "name": tc.name, "output": out})
            transcript.append({"role": "tools", "results": results})

        return {"mode": "ai", "final": final_text, "actions": actions}
