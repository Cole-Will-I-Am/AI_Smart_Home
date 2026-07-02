"""CommandRouter — the one place a command is turned into an action.

Resolves the target house, checks the permission level, applies mode/confirmation/
cross-house/rate-limit/approved-hardware gates, executes via the adapter, and writes an
audit record with a rollback token where the action is reversible. This is enforcement:
the AI can only *propose* intents; it cannot bypass any of these checks.
"""
from __future__ import annotations
from .permissions import (
    PermissionEngine, Intent, Operator, Result, CONFIRM_REQUIRED, SAFETY_CRITICAL, EXPECTED_STATE,
)
from .audit import AuditLog, AuditRecord
from .adapters.base import Adapter
from .state import StateStore


class CommandRouter:
    def __init__(self, engine: PermissionEngine, state: StateStore, adapter: Adapter, audit: AuditLog,
                 health=None) -> None:
        self.engine = engine
        self.state = state
        self.adapter = adapter
        self.audit = audit
        self.health = health   # optional HealthRegistry; gates safety-critical actuation

    def _audit(self, intent: Intent, operator: Operator, status: str, message: str,
               level: int | None, rollback: str | None = None, ctoken: str | None = None) -> Result:
        self.audit.record(AuditRecord(
            tick=self.engine.tick, operator=operator.kind, house_id=intent.house_id,
            subsystem=intent.subsystem, target=intent.target, action=intent.action,
            args=dict(intent.args), level=level, status=status, message=message, rollback_token=rollback,
        ))
        return Result(status=status, message=message, level=level, confirm_token=ctoken, rollback_token=rollback)

    def execute(self, intent: Intent, operator: Operator) -> Result:
        eng = self.engine
        level = eng.level(intent.subsystem, intent.action)

        if level is None:
            return self._audit(intent, operator, "refused", f"unknown action {intent.subsystem}.{intent.action}", None)
        if level == 5:
            return self._audit(intent, operator, "prohibited", "L5 prohibited — action blocked", 5)
        if level == 4:
            return self._audit(intent, operator, "recommend_only",
                               "L4 recommend-only — no execution path; use recommend()", 4)
        if operator.kind == "guest" and level > 1:
            return self._audit(intent, operator, "refused", "guest operator limited to Level 1", level)

        # RBAC: property scope + role capability cap (identity.py)
        if operator.houses != "*" and intent.house_id not in operator.houses:
            return self._audit(intent, operator, "refused",
                               f"property {intent.house_id} is out of scope for operator {operator.name}", level)
        if operator.max_level is not None and level > operator.max_level:
            return self._audit(intent, operator, "refused",
                               f"action level L{level} exceeds operator role (max L{operator.max_level})", level)

        # cross-house guard
        if intent.house_id != operator.active_house and not intent.confirm_cross_house:
            return self._audit(intent, operator, "confirm_required",
                               f"cross-house: confirm you intend to control {intent.house_id}", level)

        # confirmation gate
        needs_confirm = (intent.subsystem, intent.action) in CONFIRM_REQUIRED
        if operator.kind == "ai" and level >= 2:
            needs_confirm = True   # AI's L2+ control is conditioned on a human confirmation
        if needs_confirm:
            authorized = (operator.kind == "system" and intent.emergency) or eng.check_token(intent, operator)
            if not authorized:
                # The AI cannot self-confirm — a human must. So it gets no usable token,
                # only the signal that human confirmation is required. Interactive operators
                # (owner) receive a single-use, unguessable token bound to this exact intent
                # (including args) AND to their operator identity.
                tok = None if operator.kind == "ai" else eng.issue_token(intent, operator)
                return self._audit(intent, operator, "confirm_required",
                                   f"confirmation required for {intent.subsystem}.{intent.action}", level, ctoken=tok)

        # L3 requires approved, professionally-installed hardware
        if level == 3:
            ent = self.state.entity(intent.entity_id)
            if ent is None or not ent.approved_hardware:
                return self._audit(intent, operator, "refused",
                                   "L3 requires approved, professionally-installed hardware", level)

        if not eng.allow_rate(intent):
            return self._audit(intent, operator, "refused", "rate limited", level)
        if not eng.allow_cooldown(intent):
            return self._audit(intent, operator, "refused",
                               f"cooldown: {intent.subsystem}.{intent.action} actuated too recently", level)

        # Safety-critical health gate: never actuate a lock/valve/generator/HVAC-cutoff we can't
        # confirm is present and responsive.
        key = (intent.subsystem, intent.action)
        if self.health is not None and key in SAFETY_CRITICAL:
            hstatus = self.health.status(intent.entity_id, eng.tick)
            if not self.health.healthy(intent.entity_id, eng.tick):
                return self._audit(intent, operator, "refused",
                                   f"device {intent.entity_id} is {hstatus} — refusing safety-critical actuation", level)

        res = self.adapter.apply(intent)
        if not res.get("ok"):
            return self._audit(intent, operator, "refused", res.get("message", "device error"), level)

        # Verified actuation: for safety-critical actions the adapter didn't already verify, read the
        # resulting state back and require it to match the commanded outcome. A device that accepts a
        # command but doesn't move is recorded as UNVERIFIED (not executed) and flagged unhealthy.
        expect = EXPECTED_STATE.get(key)
        if expect is not None and not res.get("verified"):
            actual = self.state.get_state(intent.entity_id)
            if actual not in expect:
                if self.health is not None:
                    self.health.mark_offline(intent.entity_id)
                return self._audit(intent, operator, "unverified",
                                   f"{intent.entity_id} did not reach {expect} (state={actual!r}) — NOT confirmed", level)

        if self.health is not None:
            self.health.heartbeat(intent.entity_id, eng.tick)   # the device responded

        rollback = None
        if res.get("undo"):
            rollback = f"rb-{eng.tick}-{intent.subsystem}-{intent.target}-{len(self.audit.records)}"
            self.audit.register_rollback(rollback, res["undo"])
        return self._audit(intent, operator, "executed", res["message"], level, rollback=rollback)

    def recommend(self, house_id: str, message: str, operator: Operator, level: int = 4) -> Result:
        return self._audit(
            Intent(house_id=house_id, subsystem="advisory", target="operator", action="recommend"),
            operator, "recommended", message, level,
        )

    def rollback(self, token: str, operator: Operator | None = None) -> bool:
        undo = self.audit.rollback(token)
        ok = undo is not None
        if ok:
            self.adapter.undo(undo)
        self.audit.record(AuditRecord(
            tick=self.engine.tick, operator=(operator.kind if operator else "system"),
            house_id="n/a", subsystem="advisory", target="rollback", action="rollback",
            args={"token": token}, level=None,
            status="rollback" if ok else "refused",
            message="rollback applied" if ok else "rollback: unknown token",
        ))
        return ok
