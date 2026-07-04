"""Governed standing automations.

Routines are deterministic standing automations installed by a human owner. Every
fired step still routes through the CommandRouter/PermissionEngine, and L2+ steps
execute only when covered by routine-carried authority or standing delegation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import json
import os
import re
from typing import Any
from uuid import uuid4

from .audit import AuditRecord
from .delegations import is_delegable_action, try_delegated_execute
from .events import Event
from .permissions import ACTION_LEVELS, Intent, Operator, semantic_violation

DEFAULT_ROUTINE_BUDGET = 20


def _entity_id_for(house_id: str, raw: str) -> str:
    raw = str(raw)
    if raw.startswith("house_"):
        return raw
    if raw.count(".") == 1:
        return f"{house_id}.{raw}"
    return raw


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


def _event_matches(ev: Event, house_id: str, pred: dict, _allow_advisory_events: bool) -> tuple[bool, str | None]:
    if ev.house_id != house_id:
        return False, f"{ev.house_id} != {house_id}"
    typ = pred.get("event_type") or pred.get("type")
    if typ is not None and ev.type != typ:
        return False, f"event.type == {typ!r}"
    raw_eid = pred.get("entity_id") or pred.get("entity")
    if raw_eid is not None and ev.entity_id != _entity_id_for(house_id, raw_eid):
        return False, f"event.entity_id == {_entity_id_for(house_id, raw_eid)!r}"
    inference = pred.get("inference") or pred.get("inference_type")
    if inference is not None and ev.data.get("inference_type") != inference:
        return False, f"inference_type == {inference!r}"
    data = pred.get("data") or pred.get("data_equals") or {}
    if not isinstance(data, dict):
        return False, "event data predicate must be an object"
    for k, v in data.items():
        if not _same_value(ev.data.get(k), v):
            return False, f"event.data[{k!r}] == {v!r}"
    return True, f"event {ev.type}"


def _validate_when_shape(when) -> None:
    if when in (None, "", {}):
        return
    if isinstance(when, str):
        if not re.match(r"^\s*([A-Za-z0-9_.:-]+)\s*(==|!=)\s*(.+?)\s*$", when):
            raise ValueError("invalid when predicate")
        return
    if not isinstance(when, dict):
        raise ValueError("invalid when predicate")
    if "all" in when or "any" in when:
        key = "all" if "all" in when else "any"
        items = when[key]
        if not isinstance(items, list):
            raise ValueError(f"when '{key}' must be a list")
        for item in items:
            _validate_when_shape(item)
        return
    if "not" in when:
        _validate_when_shape(when["not"])
        return
    pred = when.get("recent_event") if "recent_event" in when else when
    if "recent_event" in when and not isinstance(pred, dict):
        raise ValueError("recent_event must be an object")
    if isinstance(pred, dict):
        typ = pred.get("event_type") or pred.get("type")
        if typ is not None or "inference" in pred or "inference_type" in pred:
            data = pred.get("data") or pred.get("data_equals") or {}
            if not isinstance(data, dict):
                raise ValueError("event data predicate must be an object")
            return
    raw_eid = when.get("entity_id") or when.get("entity") or when.get("target")
    if not raw_eid:
        raise ValueError("when predicate requires entity_id")
    supported = {"equals", "eq", "state", "not_equals", "ne", "in", "not_in"}
    if not any(k in when for k in supported):
        raise ValueError("unsupported when predicate")
    if "in" in when and not isinstance(when["in"], list):
        raise ValueError("when 'in' must be a list")
    if "not_in" in when and not isinstance(when["not_in"], list):
        raise ValueError("when 'not_in' must be a list")


def eval_when(world, house_id: str, when, trigger_event: Event | None = None,
              allow_advisory_events: bool = True) -> tuple[bool, str | None]:
    """Evaluate the plan/routine predicate style against live state and recent events."""
    if when in (None, "", {}):
        return True, None
    if isinstance(when, str):
        m = re.match(r"^\s*([A-Za-z0-9_.:-]+)\s*(==|!=)\s*(.+?)\s*$", when)
        if not m:
            return False, "invalid when predicate"
        eid, op, raw = m.groups()
        actual = world.state.get_state(_entity_id_for(house_id, eid))
        expected = _literal(raw)
        ok = _same_value(actual, expected)
        return (ok if op == "==" else not ok), f"{_entity_id_for(house_id, eid)} {op} {expected!r}"
    if not isinstance(when, dict):
        return False, "invalid when predicate"

    if "all" in when:
        items = when["all"]
        if not isinstance(items, list):
            return False, "when 'all' must be a list"
        reasons = []
        for item in items:
            ok, why = eval_when(world, house_id, item, trigger_event, allow_advisory_events)
            reasons.append(why)
            if not ok:
                return False, why
        return True, "; ".join(r for r in reasons if r)
    if "any" in when:
        items = when["any"]
        if not isinstance(items, list):
            return False, "when 'any' must be a list"
        reasons = []
        for item in items:
            ok, why = eval_when(world, house_id, item, trigger_event, allow_advisory_events)
            reasons.append(why)
            if ok:
                return True, why
        return False, "; ".join(r for r in reasons if r)
    if "not" in when:
        ok, why = eval_when(world, house_id, when["not"], trigger_event, allow_advisory_events)
        return not ok, f"not ({why})" if why else "not predicate"

    if "recent_event" in when:
        pred = when["recent_event"]
        if not isinstance(pred, dict):
            return False, "recent_event must be an object"
        try:
            within = int(pred.get("within_ticks", pred.get("within", 20)))
        except (TypeError, ValueError):
            return False, "recent_event within_ticks must be numeric"
        cutoff = world.engine.tick - max(0, within)
        for ev in reversed(world.bus.history):
            if ev.tick < cutoff:
                continue
            ok, why = _event_matches(ev, house_id, pred, allow_advisory_events)
            if ok:
                return True, why
        return False, "no matching recent event"

    if "event_type" in when or "type" in when or "inference" in when or "inference_type" in when:
        if trigger_event is None:
            return False, "event predicate requires an event"
        return _event_matches(trigger_event, house_id, when, allow_advisory_events)

    raw_eid = when.get("entity_id") or when.get("entity") or when.get("target")
    if not raw_eid:
        return False, "when predicate requires entity_id"
    eid = _entity_id_for(house_id, raw_eid)
    actual = world.state.get_state(eid)
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


def _intent_from_step(default_house: str, step: dict) -> tuple[Intent | None, str | None]:
    missing = [k for k in ("subsystem", "target", "action") if not step.get(k)]
    if missing:
        return None, f"malformed routine step: missing {', '.join(missing)}"
    raw_args = step.get("args", {})
    if not isinstance(raw_args, dict):
        return None, f"malformed routine step: args must be an object, got {type(raw_args).__name__}"
    return Intent(
        house_id=step.get("house_id") or default_house,
        subsystem=str(step["subsystem"]),
        target=str(step["target"]),
        action=str(step["action"]),
        args=dict(raw_args),
    ), None


def _step_to_spec(default_house: str, step: dict) -> dict:
    intent, err = _intent_from_step(default_house, step)
    if err is not None or intent is None:
        raise ValueError(err or "malformed routine step")
    return {
        "house_id": intent.house_id,
        "subsystem": intent.subsystem,
        "target": intent.target,
        "action": intent.action,
        "args": dict(intent.args),
    }


@dataclass
class Routine:
    id: str
    when: Any
    then_steps: list[dict]
    grantor: str
    house_id: str
    window: tuple[int, int] | None = None
    budget_per_day: int = DEFAULT_ROUTINE_BUDGET
    expires: date | None = None
    authority_max_level: int | None = None
    revoked: bool = False
    last_fired_tick: int | None = None
    last_results: list[dict] = field(default_factory=list)
    used_today: int = 0
    _day: date | None = field(default=None, repr=False)

    def _roll(self, now: datetime) -> None:
        if self._day != now.date():
            self._day, self.used_today = now.date(), 0

    def budget_remaining(self, now: datetime) -> int:
        if self._day != now.date():
            return max(0, self.budget_per_day)
        return max(0, self.budget_per_day - self.used_today)

    def can_fire(self, now: datetime) -> bool:
        if self.revoked or (self.expires is not None and now.date() > self.expires):
            return False
        if self.window is not None:
            s, e = self.window
            h = now.hour
            if not ((h >= s or h <= e) if s > e else (s <= h <= e)):
                return False
        self._roll(now)
        return self.used_today < self.budget_per_day

    def mark_fired(self, tick: int, now: datetime, results: list[dict]) -> None:
        self._roll(now)
        self.used_today += 1
        self.last_fired_tick = tick
        self.last_results = list(results)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "when": self.when,
            "then_steps": list(self.then_steps),
            "grantor": self.grantor,
            "house_id": self.house_id,
            "window": list(self.window) if self.window else None,
            "budget_per_day": self.budget_per_day,
            "expires": self.expires.isoformat() if self.expires else None,
            "authority_max_level": self.authority_max_level,
            "revoked": self.revoked,
            "last_fired_tick": self.last_fired_tick,
            "last_results": list(self.last_results),
            "used_today": self.used_today,
            "day": self._day.isoformat() if self._day else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Routine":
        return cls(
            id=d["id"],
            when=d.get("when"),
            then_steps=list(d.get("then_steps", [])),
            grantor=d.get("grantor", ""),
            house_id=d["house_id"],
            window=tuple(d["window"]) if d.get("window") else None,
            budget_per_day=int(d.get("budget_per_day", DEFAULT_ROUTINE_BUDGET)),
            expires=date.fromisoformat(d["expires"]) if d.get("expires") else None,
            authority_max_level=(None if d.get("authority_max_level") is None
                                 else int(d.get("authority_max_level"))),
            revoked=bool(d.get("revoked", False)),
            last_fired_tick=d.get("last_fired_tick"),
            last_results=list(d.get("last_results", [])),
            used_today=int(d.get("used_today", 0)),
            _day=date.fromisoformat(d["day"]) if d.get("day") else None,
        )


class RoutineRegistry:
    def __init__(self, clock=None, path: str | None = None) -> None:
        self.clock = clock or datetime.now
        self.world = None
        self._routines: dict[str, Routine] = {}
        self._path = path
        if path and os.path.exists(path):
            self._load()

    def attach(self, world) -> "RoutineRegistry":
        self.world = world
        world.bus.subscribe(self._on_event)
        return self

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([r.to_dict() for r in self._routines.values()], f, default=str)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def _load(self) -> None:
        with open(self._path) as f:
            for e in json.load(f):
                r = Routine.from_dict(e)
                self._routines[r.id] = r

    def persist(self) -> None:
        self._save()

    def _validate(self, r: Routine, by: Operator | None = None) -> None:
        if self.world is None:
            raise RuntimeError("RoutineRegistry must be attached to a world before install")
        if r.house_id not in self.world.houses:
            raise ValueError(f"unknown house {r.house_id!r}")
        if not isinstance(r.then_steps, list) or not r.then_steps:
            raise ValueError("routine requires at least one then step")
        if r.budget_per_day < 1:
            raise ValueError("routine budget_per_day must be >= 1")
        if r.authority_max_level is not None:
            if isinstance(r.authority_max_level, bool) or not isinstance(r.authority_max_level, int):
                raise ValueError("routine authority_max_level must be an integer 0..3 or None")
            if r.authority_max_level < 0 or r.authority_max_level > 3:
                raise ValueError("routine authority_max_level must be between 0 and 3")
            if by is not None and by.max_level is not None and r.authority_max_level > by.max_level:
                raise PermissionError(
                    f"installer role caps at L{by.max_level}; cannot grant routine authority L{r.authority_max_level}")
        _validate_when_shape(r.when)
        if r.window is not None:
            if len(r.window) != 2 or any(not isinstance(v, int) or v < 0 or v > 23 for v in r.window):
                raise ValueError("routine window must be (start_hour, end_hour)")
        for idx, step in enumerate(r.then_steps):
            if not isinstance(step, dict):
                raise ValueError(f"routine step {idx} must be an object")
            intent, err = _intent_from_step(r.house_id, step)
            if err is not None or intent is None:
                raise ValueError(err or f"malformed routine step {idx}")
            if intent.house_id != r.house_id:
                raise ValueError("routine steps must stay within the routine house")
            lvl = ACTION_LEVELS.get((intent.subsystem, intent.action))
            if lvl is None:
                raise ValueError(f"cannot install routine with unknown action {intent.subsystem}.{intent.action}")
            if lvl >= 4:
                raise ValueError(f"L{lvl} actions may not appear in a routine")
            if by is not None and by.max_level is not None and lvl > by.max_level:
                raise PermissionError(f"installer role caps at L{by.max_level}; cannot install L{lvl} routine step")

    def install(self, r: Routine, by: Operator) -> Routine:
        if by is None or getattr(by, "kind", None) != "owner":
            raise PermissionError("only an owner may install a standing routine")
        if by.houses != "*" and r.house_id not in by.houses:
            raise PermissionError(f"installer {by.name or 'owner'!r} has no authority over {r.house_id}")
        installer = by.name or "owner"
        if r.grantor and r.grantor != installer:
            raise PermissionError(f"routine grantor {r.grantor!r} does not match installer {installer!r}")
        if not r.grantor:
            r.grantor = installer
        r.then_steps = [_step_to_spec(r.house_id, s) for s in r.then_steps]
        self._validate(r, by)
        self._routines[r.id] = r
        self._save()
        return r

    def revoke(self, routine_id: str) -> bool:
        r = self._routines.get(routine_id)
        if r is not None:
            r.revoked = True
            self._save()
        return r is not None

    def propose_spec(self, house_id: str, when, then_steps: list[dict],
                     authority_max_level: int | None = None) -> dict:
        if self.world is None:
            raise RuntimeError("RoutineRegistry must be attached to a world before proposing specs")
        r = Routine(
            id=f"rt-{house_id}-{uuid4().hex[:8]}",
            when=when,
            then_steps=list(then_steps),
            grantor="",
            house_id=house_id,
            budget_per_day=DEFAULT_ROUTINE_BUDGET,
            authority_max_level=authority_max_level,
        )
        r.then_steps = [_step_to_spec(house_id, s) for s in r.then_steps]
        self._validate(r)
        d = r.to_dict()
        d["grantor"] = "<resident-owner>"
        d["install_requires"] = "human_owner"
        return d

    def _on_event(self, ev: Event) -> None:
        self.evaluate(trigger_event=ev)

    def evaluate_tick(self) -> None:
        self.evaluate(trigger_event=None)

    def evaluate(self, trigger_event: Event | None = None) -> list[dict]:
        if self.world is None:
            return []
        outcomes = []
        for r in list(self._routines.values()):
            out = self._maybe_fire(r, trigger_event)
            if out is not None:
                outcomes.append(out)
        return outcomes

    def _maybe_fire(self, r: Routine, trigger_event: Event | None) -> dict | None:
        assert self.world is not None
        if trigger_event is not None and trigger_event.house_id != r.house_id:
            return None
        now = self.clock()
        if not r.can_fire(now):
            return None
        ok, why = eval_when(self.world, r.house_id, r.when, trigger_event, allow_advisory_events=True)
        if not ok:
            return None
        results = [self._execute_step(r, idx, step) for idx, step in enumerate(r.then_steps)]
        r.mark_fired(self.world.engine.tick, now, results)
        self._audit_fire(r, trigger_event, why, results)
        self._save()
        return {"routine_id": r.id, "results": results}

    def _execute_step(self, r: Routine, idx: int, step: dict) -> dict:
        assert self.world is not None
        intent, err = _intent_from_step(r.house_id, step)
        if err is not None or intent is None:
            return {"index": idx, "status": "refused", "level": None, "message": err or "malformed step"}
        lvl = self.world.router.engine.level(intent.subsystem, intent.action)
        if lvl is not None and lvl >= 2:
            inline = self._try_routine_authority_execute(r, idx, intent, lvl)
            if inline is not None:
                return inline
            if self.world.delegations is not None:
                d_res, d = try_delegated_execute(self.world, intent, self.world.delegations, grantor=r.grantor)
                if d_res is not None:
                    return {
                        "index": idx,
                        "status": d_res.status,
                        "message": d_res.message,
                        "level": d_res.level,
                        "delegation": d.id,
                        "intent": self._intent_dict(intent),
                    }
        if lvl is not None and lvl >= 2:
            # Route through the engine as an AI-originated pending action so L2+ cannot
            # execute merely because a routine exists. Routine-carried authority and
            # standing delegation are the autonomous execution paths for L2/L3.
            op = Operator("ai", r.house_id, f"routine:{r.id}:{r.grantor}")
        else:
            op = Operator("owner", r.house_id, f"routine:{r.id}:{r.grantor}")
        res = self.world.router.execute(intent, op)
        return {
            "index": idx,
            "status": res.status,
            "message": res.message,
            "level": res.level,
            "intent": self._intent_dict(intent),
        }

    def _try_routine_authority_execute(self, r: Routine, idx: int, intent: Intent, lvl: int) -> dict | None:
        assert self.world is not None
        cap = r.authority_max_level
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0 or cap > 3:
            return None
        if lvl > cap or not is_delegable_action(intent.subsystem, intent.action):
            return None
        delegation_id = f"routine:{r.id}:authority"
        grantor_op = Operator(kind="owner", active_house=intent.house_id,
                              name=f"{delegation_id}:{r.grantor}")
        if semantic_violation(intent, grantor_op, self.world.router.clock()) is not None:
            return None
        eng = self.world.router.engine
        intent.confirm_token = eng.issue_token(intent, grantor_op)
        try:
            res = self.world.router.execute(intent, grantor_op)
        finally:
            eng.consume_token(intent)
            intent.confirm_token = None
        if not res.ok:
            return None
        self._audit_routine_authority(r, idx, intent, lvl, delegation_id)
        return {
            "index": idx,
            "status": res.status,
            "message": res.message,
            "level": res.level,
            "delegation": delegation_id,
            "intent": self._intent_dict(intent),
        }

    @staticmethod
    def _intent_dict(intent: Intent) -> dict:
        return {
            "house_id": intent.house_id,
            "subsystem": intent.subsystem,
            "target": intent.target,
            "action": intent.action,
            "args": dict(intent.args),
        }

    def _audit_fire(self, r: Routine, trigger_event: Event | None, why: str | None, results: list[dict]) -> None:
        assert self.world is not None
        trigger = None
        if trigger_event is not None:
            trigger = {"type": trigger_event.type, "entity_id": trigger_event.entity_id, "tick": trigger_event.tick}
        self.world.router.audit.record(AuditRecord(
            tick=self.world.engine.tick,
            operator="owner",
            house_id=r.house_id,
            subsystem="routine",
            target=r.id,
            action="fire",
            args={
                "grantor": r.grantor,
                "when": r.when,
                "matched": why,
                "trigger": trigger,
                "results": results,
                "used_today": r.used_today,
                "budget_per_day": r.budget_per_day,
            },
            level=None,
            status="routine_fired",
            message=f"routine {r.id} fired under {r.grantor}",
        ))

    def _audit_routine_authority(self, r: Routine, idx: int, intent: Intent, lvl: int, delegation_id: str) -> None:
        assert self.world is not None
        self.world.router.audit.record(AuditRecord(
            tick=self.world.engine.tick,
            operator="owner",
            house_id=intent.house_id,
            subsystem="advisory",
            target=delegation_id,
            action="delegation_used",
            args={
                "grantor": r.grantor,
                "routine_id": r.id,
                "step_index": idx,
                "covered": f"{intent.subsystem}.{intent.target}.{intent.action}",
                "args": dict(intent.args),
                "authority_max_level": r.authority_max_level,
                "used_today": r.used_today + 1,
                "budget": r.budget_per_day,
            },
            level=lvl,
            status="delegated",
            message=f"routine authority {r.id} executed {intent.subsystem}.{intent.action} for {r.grantor}",
        ))

    def list(self, house_id: str | None = None) -> list[Routine]:
        return [r for r in self._routines.values() if house_id is None or r.house_id == house_id]

    def to_public(self, house_id: str | None = None) -> list[dict]:
        now = self.clock()
        out = []
        for r in self.list(house_id):
            out.append({
                "id": r.id,
                "house_id": r.house_id,
                "when": r.when,
                "then_steps": list(r.then_steps),
                "grantor": r.grantor,
                "window": list(r.window) if r.window else None,
                "budget_per_day": r.budget_per_day,
                "budget_remaining": r.budget_remaining(now),
                "expires": r.expires.isoformat() if r.expires else None,
                "authority_max_level": r.authority_max_level,
                "revoked": r.revoked,
                "last_fired_tick": r.last_fired_tick,
                "last_results": list(r.last_results),
            })
        return out

    def __iter__(self):
        return iter(self._routines.values())

    def __len__(self) -> int:
        return len(self._routines)
