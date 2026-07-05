"""Conformance tier — the safety case, made live.

safety_case.py binds each safety claim to pytest node ids: the claim "the AI can never
execute an L4/L5 action" is a green test at commission time. That proves the *code* admits
no such path. It does not witness the *running estate* — nothing re-checks, tick by tick,
that the deployed system is still honouring those properties as real events flow.

This tier closes that gap. It expresses a small set of safety properties as bounded-time
temporal invariants over the live audit + event stream and evaluates them every tick:

    G(fire_verified  -> F<=N egress_unlocked)         a verified fire must unlock egress
    G(leak_verified  -> F<=N main_closed | escalated)  a verified leak must close the main
                                                        (or be escalated to a human)
    G(ai & level>=L4 -> never executed)                the AI never executes an L4/L5 deed

Each is a pure reducer over records the engine already emits — no new authority, no
actuation. A satisfied invariant is silent. A *violated* invariant is a first-class,
hash-chained `conformance_violation` incident, and — because a breached safety property
means the house is no longer demonstrably safe under autonomy — it asks the House Director
to escalate to a human. The monitor governs nothing directly; it witnesses, records, and
hands control up. Safety stops being a claim that was green once and becomes a property the
system continuously proves about itself, or loudly admits it cannot.

The invariants are intentionally derived from existing safety-case claims (SC-1, plus the
two headline life-safety automations), so their coverage is bounded by something already
audited rather than invented ad hoc — the standing critique of runtime monitors ("a
glorified assert with incomplete specs") is answered by tying the spec to the safety case.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .audit import AuditRecord
from .events import Event


@dataclass(frozen=True)
class Invariant:
    """A bounded-response safety property: once its trigger holds at tick t, the response
    must hold at some tick in [t, t+deadline]. If the deadline passes unmet, it is violated.
    `escalates` marks properties whose breach should raise the Director."""
    id: str
    prop: str
    deadline: int
    claim: str = ""          # the safety_case claim id this invariant operationalizes
    escalates: bool = True


# Human-legible catalogue. Kept tiny and tied to safety-case claims on purpose.
INVARIANTS: tuple[Invariant, ...] = (
    Invariant("INV-FIRE-EGRESS",
              "verified fire/CO must unlock designated egress within the deadline",
              deadline=3, claim="life-safety automation #8"),
    Invariant("INV-LEAK-MAIN",
              "verified leak must close the water main or escalate to a human within the deadline",
              deadline=3, claim="life-safety automation #1"),
    Invariant("INV-AI-L4",
              "an AI-originated L4/L5 action must never execute",
              deadline=0, claim="SC-1"),
)


@dataclass
class _Pending:
    inv_id: str
    house_id: str
    opened_tick: int
    detail: dict


@dataclass
class ConformanceMonitor:
    """Bus-attached runtime monitor over audit records + events. Pure reducer plus a small
    set of open obligations; its only effects are recording an incident and asking the
    Director to escalate. It never actuates."""
    world: object | None = None
    invariants: tuple[Invariant, ...] = INVARIANTS
    _pending: list[_Pending] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)

    def attach(self, world) -> "ConformanceMonitor":
        self.world = world
        world.bus.subscribe(self._on_event)
        return self

    def _clock(self) -> int:
        return self.world.engine.tick if self.world else 0

    # --- obligation lifecycle --------------------------------------------------------
    def _open(self, inv_id: str, house_id: str, detail: dict) -> None:
        if any(p.inv_id == inv_id and p.house_id == house_id for p in self._pending):
            return                                   # one open obligation per (inv, house)
        self._pending.append(_Pending(inv_id, house_id, self._clock(), detail))

    def _discharge(self, inv_id: str, house_id: str) -> None:
        self._pending = [p for p in self._pending
                         if not (p.inv_id == inv_id and p.house_id == house_id)]

    def _on_event(self, ev: Event) -> None:
        if self.world is None or ev.type == "conformance_violation":
            return
        h = ev.house_id
        if ev.type == "smoke_co" and ev.data.get("verified"):
            self._open("INV-FIRE-EGRESS", h, {"cause": "fire/CO verified"})
        if ev.type == "leak":
            wet = bool(ev.entity_id) and self.world.state.get_state(ev.entity_id) == "wet"
            if wet:
                self._open("INV-LEAK-MAIN", h, {"cause": "leak signal, sensor wet"})
        self.evaluate()

    def evaluate(self) -> list[dict]:
        """Discharge met obligations, fire violations for expired ones, and continuously
        check the standing (deadline-0) invariant. Wired into World.tick and also run on
        every event so a same-tick discharge is seen before the deadline is judged."""
        if self.world is None:
            return []
        now = self._clock()
        fired: list[dict] = []

        # INV-AI-L4: standing invariant — any EXECUTED AI record at level >= 4 is a breach.
        for r in self.world.audit.records:
            if r.status == "executed" and (r.level or 0) >= 4 and _is_ai(r.operator):
                fired.append(self._violate("INV-AI-L4", r.house_id, now, {"record": _brief(r)}))

        # Bounded-response obligations: discharge if met, else fire once the deadline passes.
        for p in list(self._pending):
            if self._satisfied(p):
                self._discharge(p.inv_id, p.house_id)
            elif now - p.opened_tick > _deadline(p.inv_id, self.invariants):
                fired.append(self._violate(p.inv_id, p.house_id, now, p.detail))
                self._discharge(p.inv_id, p.house_id)
        return fired

    def _satisfied(self, p: _Pending) -> bool:
        st = self.world.state
        if p.inv_id == "INV-FIRE-EGRESS":
            return st.get_state(f"{p.house_id}.lock.egress_side") == "unlocked"
        if p.inv_id == "INV-LEAK-MAIN":
            if st.get_state(f"{p.house_id}.water.main_valve") == "closed":
                return True
            d = getattr(self.world, "director", None)   # escalation is the safe alternative
            if d is not None:
                from .director import DirectorState
                return d.state(p.house_id) != DirectorState.AUTONOMOUS
            return False
        return False

    def _violate(self, inv_id: str, house_id: str, now: int, detail: dict) -> dict:
        inv = next(i for i in self.invariants if i.id == inv_id)
        standing = inv.deadline == 0
        for v in self.violations:                      # de-dupe standing breaches
            if v["invariant"] == inv_id and v["house_id"] == house_id and v.get("standing"):
                return v
        rec = {"invariant": inv_id, "house_id": house_id, "tick": now,
               "property": inv.prop, "claim": inv.claim, "detail": detail, "standing": standing}
        self.violations.append(rec)
        self.world.audit.record(AuditRecord(
            tick=now, operator="monitor", house_id=house_id, subsystem="conformance",
            target=inv_id, action="violation", args=dict(detail), level=None,
            status="conformance_violation",
            message=f"conformance breach {inv_id}: {inv.prop}"))
        self.world.bus.publish(Event("conformance_violation", house_id, None, rec, now))
        if inv.escalates:
            d = getattr(self.world, "director", None)
            if d is not None:
                from .director import Trigger
                d.escalate(house_id, Trigger.LIFE_SAFETY_INFERENCE,
                           {"kind": "fire" if "FIRE" in inv_id else "leak",
                            "reason": f"conformance:{inv_id}", "property": inv.prop})
        return rec


def _is_ai(kind) -> bool:
    return str(kind).lower() == "ai"


def _deadline(inv_id: str, invs: tuple[Invariant, ...]) -> int:
    return next(i.deadline for i in invs if i.id == inv_id)


def _brief(r) -> dict:
    return {"target": r.target, "action": r.action, "level": r.level, "status": r.status}
