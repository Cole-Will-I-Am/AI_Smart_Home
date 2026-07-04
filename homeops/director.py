"""homeops.director — the House Director: a per-house operating-mode FSM above the engine.

Adapted from the Matrix Tri-State Director. It governs the house's *mode* — who is driving
right now — and NOTHING else. It executes nothing and authorizes nothing; every AI action
still faces the permission engine unchanged. Its only side effects are: (a) recording a
hash-chained transition, and (b) tying HUMAN_OVERRIDE to the existing per-house ai_hold.

  AUTONOMOUS      routines + delegations run; the engine gates every action (default).
  AI_ACTIVE       an escalation is being worked, under a per-episode containment budget.
  HUMAN_OVERRIDE  AI actuation suspended (== ai_hold); local automations + physical controls run.

Transitions:
  AUTONOMOUS     --(escalation: evidence-validated, past cooldown)--> AI_ACTIVE
  AI_ACTIVE      --(resolved | timeout | budget exhausted)---------> AUTONOMOUS
  ANY (human)    --(enter override)-------------------------------> HUMAN_OVERRIDE
  HUMAN_OVERRIDE --(human release)--------------------------------> AUTONOMOUS
"""
from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from .audit import AuditRecord
from .events import Event


class DirectorState(Enum):
    AUTONOMOUS = "autonomous"
    AI_ACTIVE = "ai_active"
    HUMAN_OVERRIDE = "human_override"


class Trigger(Enum):
    LIFE_SAFETY_INFERENCE = "life_safety_inference"
    HEALTH_CASCADE = "health_cascade"
    ACTUATION_FAILURE_RATE = "actuation_failure_rate"
    BRIDGE_LOSS = "bridge_loss"
    DELEGATION_EXHAUSTED = "delegation_exhausted"
    MANUAL_ESCALATE = "manual_escalate"


# inference_type / raw event types that count as life-safety
LIFE_SAFETY_KINDS = {"leak_suspected", "ventilation_fault", "fire", "smoke_co", "leak"}
# audit statuses that mean the house tried and failed to effect physical change
FAILURE_STATUSES = {"unverified", "prohibited"}


@dataclass(frozen=True)
class SemanticDelta:
    """A frozen snapshot of *why* the house escalated. Assembled once, then immutable."""
    id: str
    trigger: Trigger
    house_id: str
    tick: int
    evidence: dict

    def to_dict(self) -> dict:
        return {"id": self.id, "trigger": self.trigger.value, "house_id": self.house_id,
                "tick": self.tick, "evidence": self.evidence}


def validate_for_trigger(delta: SemanticDelta) -> bool:
    """The evidence must justify the claimed trigger — a spurious escalation never changes mode."""
    e = delta.evidence or {}
    t = delta.trigger
    if t is Trigger.MANUAL_ESCALATE:
        return bool(e.get("by"))
    if t is Trigger.LIFE_SAFETY_INFERENCE:
        return e.get("kind") in LIFE_SAFETY_KINDS
    if t is Trigger.HEALTH_CASCADE:
        n = int(e.get("offline_count", 0))
        return n > 0 and n >= int(e.get("threshold", 1))
    if t is Trigger.ACTUATION_FAILURE_RATE:
        n = int(e.get("failures", 0))
        return n > 0 and n >= int(e.get("threshold", 1))
    if t is Trigger.BRIDGE_LOSS:
        return bool(e.get("bridge_down") or e.get("wan_down"))
    if t is Trigger.DELEGATION_EXHAUSTED:
        return bool(e.get("delegation_id"))
    return False


@dataclass
class ContainmentPolicy:
    """Bounds the AI while AI_ACTIVE on THIS episode — distinct from standing budgets."""
    action_budget: int = 8
    permitted_tools: Any = "*"          # "*" or a set of tool names
    timeout_ticks: int = 20
    used: int = 0

    def permits(self, tool: str) -> bool:
        return self.permitted_tools == "*" or tool in self.permitted_tools

    def consume(self) -> bool:
        if self.used >= self.action_budget:
            return False
        self.used += 1
        return True

    def remaining(self) -> int:
        return max(0, self.action_budget - self.used)


@dataclass
class HouseMode:
    house_id: str
    state: DirectorState = DirectorState.AUTONOMOUS
    since_tick: int = 0
    last_escalation_tick: int | None = None
    delta: SemanticDelta | None = None
    containment: ContainmentPolicy | None = None


def _is_human(op) -> bool:
    return op is not None and getattr(op, "kind", None) in ("owner", "system")


class Director:
    """Deterministic per-house operating-mode FSM. The LLM is an optional worker, never required."""

    def __init__(self, world=None, *, cooldown_ticks: int = 10, timeout_ticks: int = 20,
                 containment_budget: int = 8, health_cascade_threshold: int = 2,
                 failure_window_ticks: int = 20, failure_threshold: int = 3,
                 on_transition: Callable | None = None) -> None:
        self.world = world
        self.cooldown_ticks = cooldown_ticks
        self.timeout_ticks = timeout_ticks
        self.containment_budget = containment_budget
        self.health_cascade_threshold = health_cascade_threshold
        self.failure_window_ticks = failure_window_ticks
        self.failure_threshold = failure_threshold
        self.on_transition = on_transition
        self._modes: dict[str, HouseMode] = {}

    def attach(self, world) -> "Director":
        self.world = world
        world.bus.subscribe(self._on_event)
        return self

    # --- accessors -----------------------------------------------------------------
    def _clock(self) -> int:
        return self.world.engine.tick

    def _mode(self, house_id: str) -> HouseMode:
        m = self._modes.get(house_id)
        if m is None:
            m = self._modes[house_id] = HouseMode(house_id, since_tick=self._clock())
        return m

    def state(self, house_id: str) -> DirectorState:
        return self._mode(house_id).state

    def containment(self, house_id: str) -> ContainmentPolicy | None:
        return self._mode(house_id).containment

    def snapshot(self) -> dict:
        return {hid: {"state": m.state.value, "since_tick": m.since_tick,
                      "delta": m.delta.to_dict() if m.delta else None,
                      "budget_remaining": m.containment.remaining() if m.containment else None}
                for hid, m in self._modes.items()}

    # --- transition + audit --------------------------------------------------------
    def _transition(self, house_id: str, to: DirectorState, trigger: Trigger | None,
                    delta: SemanticDelta | None, by: str = "director") -> None:
        m = self._mode(house_id)
        frm = m.state
        m.state = to
        m.since_tick = self._clock()
        m.delta = delta
        house = self.world.houses.get(house_id)
        if house is not None:                          # HUMAN_OVERRIDE <-> ai_hold
            house.ai_hold = (to is DirectorState.HUMAN_OVERRIDE)
        self.world.audit.record(AuditRecord(
            tick=self._clock(), operator=by, house_id=house_id, subsystem="director",
            target="mode", action="transition", level=None, status="director_transition",
            args={"from": frm.value, "to": to.value,
                  "trigger": trigger.value if trigger else None,
                  "delta": delta.id if delta else None},
            message=f"director {house_id}: {frm.value} -> {to.value}"
                    + (f" ({trigger.value})" if trigger else "")))
        if self.on_transition:
            self.on_transition(house_id, frm, to, trigger, delta)

    # --- escalation (AUTONOMOUS -> AI_ACTIVE) --------------------------------------
    def escalate(self, house_id: str, trigger: Trigger, evidence: dict) -> bool:
        m = self._mode(house_id)
        if m.state is DirectorState.HUMAN_OVERRIDE:
            return False                               # a human holds control; no auto-escalation
        delta = SemanticDelta("sd-" + _secrets.token_hex(4), trigger, house_id, self._clock(), dict(evidence))
        if not validate_for_trigger(delta):
            return False                               # I3: evidence must justify the trigger
        if trigger is not Trigger.MANUAL_ESCALATE and m.last_escalation_tick is not None \
                and self._clock() - m.last_escalation_tick < self.cooldown_ticks:
            return False                               # I4: cooldown prevents flapping
        if m.state is DirectorState.AI_ACTIVE:
            return False                               # already working a situation
        m.last_escalation_tick = self._clock()
        m.containment = ContainmentPolicy(action_budget=self.containment_budget,
                                          timeout_ticks=self.timeout_ticks)
        self._transition(house_id, DirectorState.AI_ACTIVE, trigger, delta)
        return True

    def de_escalate(self, house_id: str, reason: str = "resolved") -> bool:
        m = self._mode(house_id)
        if m.state is not DirectorState.AI_ACTIVE:
            return False
        m.containment = None
        self._transition(house_id, DirectorState.AUTONOMOUS, None, None, by=f"director:{reason}")
        return True

    def consume_containment(self, house_id: str) -> bool:
        """One tool/proposal call while working the escalation. False (and de-escalate) when spent."""
        m = self._mode(house_id)
        if m.state is not DirectorState.AI_ACTIVE or m.containment is None:
            return True
        if not m.containment.consume():
            self.de_escalate(house_id, "budget_exhausted")
            return False
        return True

    # --- human override (I7: humans only) ------------------------------------------
    def enter_override(self, house_id: str, by) -> None:
        if not _is_human(by):
            raise PermissionError("only a human owner may place a house in HUMAN_OVERRIDE")
        self._mode(house_id).containment = None
        self._transition(house_id, DirectorState.HUMAN_OVERRIDE, Trigger.MANUAL_ESCALATE, None,
                         by=getattr(by, "name", "owner"))

    def release_override(self, house_id: str, by) -> None:
        if not _is_human(by):
            raise PermissionError("only a human owner may release HUMAN_OVERRIDE")
        if self._mode(house_id).state is not DirectorState.HUMAN_OVERRIDE:
            return
        self._transition(house_id, DirectorState.AUTONOMOUS, None, None, by=getattr(by, "name", "owner"))

    def manual_escalate(self, house_id: str, by) -> bool:
        if not _is_human(by):
            raise PermissionError("only a human may manually escalate")
        return self.escalate(house_id, Trigger.MANUAL_ESCALATE, {"by": getattr(by, "name", "owner")})

    # --- automatic detection (deterministic) ---------------------------------------
    def _on_event(self, ev: Event) -> None:
        if self.world is None or not ev.house_id:
            return
        kind = ev.data.get("inference_type") if ev.type == "inference" else (
            ev.type if ev.type in ("smoke_co", "leak") else None)
        if kind in LIFE_SAFETY_KINDS:
            self.escalate(ev.house_id, Trigger.LIFE_SAFETY_INFERENCE,
                          {"kind": kind, "event": ev.type, "entity": ev.entity_id})

    def evaluate(self) -> None:
        """Called each tick. Deterministic auto-detection from local signals + AI_ACTIVE timeout."""
        if self.world is None:
            return
        now = self._clock()
        for house_id, house in self.world.houses.items():
            m = self._mode(house_id)
            if m.state is DirectorState.AI_ACTIVE and m.containment is not None \
                    and now - m.since_tick >= m.containment.timeout_ticks:
                self.de_escalate(house_id, "timeout")
                continue
            if m.state is not DirectorState.AUTONOMOUS:
                continue
            offline = self._offline_count(house_id, now)
            if offline >= self.health_cascade_threshold:
                self.escalate(house_id, Trigger.HEALTH_CASCADE,
                              {"offline_count": offline, "threshold": self.health_cascade_threshold})
                continue
            fails = self._recent_failures(house_id, now)
            if fails >= self.failure_threshold:
                self.escalate(house_id, Trigger.ACTUATION_FAILURE_RATE,
                              {"failures": fails, "threshold": self.failure_threshold})

    def _offline_count(self, house_id: str, now: int) -> int:
        house = self.world.houses.get(house_id)
        if house is None:
            return 0
        h = self.world.health
        return sum(1 for eid in house.entities if h.status(eid, now) in ("offline", "stale"))

    def _recent_failures(self, house_id: str, now: int) -> int:
        return sum(1 for r in self.world.audit.records
                   if r.house_id == house_id and r.status in FAILURE_STATUSES
                   and now - r.tick <= self.failure_window_ticks)
