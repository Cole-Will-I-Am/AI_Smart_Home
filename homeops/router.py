"""CommandRouter — the one place a command is turned into an action.

Resolves the target house, checks the permission level, applies mode/confirmation/
cross-house/rate-limit/approved-hardware gates, executes via the adapter, and writes an
audit record with a rollback token where the action is reversible. This is enforcement:
the AI can only *propose* intents; it cannot bypass any of these checks.
"""
from __future__ import annotations
from .permissions import (
    PermissionEngine, Intent, Operator, Result, CONFIRM_REQUIRED,
)
from .audit import AuditLog, AuditRecord
from .adapters.base import Adapter
from .state import StateStore


class CommandRouter:
    def __init__(self, engine: PermissionEngine, state: StateStore, adapter: Adapter, audit: AuditLog) -> None:
        self.engine = engine
        self.state = state
        self.adapter = adapter
        self.audit = audit

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

        # cross-house guard
        if intent.house_id != operator.active_house and not intent.confirm_cross_house:
            return self._audit(intent, operator, "confirm_required",
                               f"cross-house: confirm you intend to control {intent.house_id}", level)

        # confirmation gate
        needs_confirm = (intent.subsystem, intent.action) in CONFIRM_REQUIRED
        if operator.kind == "ai" and level >= 2:
            needs_confirm = True   # AI's L2+ control is conditioned on a human confirmation
        if needs_confirm:
            authorized = (operator.kind == "system" and intent.emergency) or eng.check_token(intent)
            if not authorized:
                # The AI cannot self-confirm — a human must. So it gets no usable token,
                # only the signal that human confirmation is required. Interactive operators
                # (owner) receive a single-use token to re-submit with.
                tok = None if operator.kind == "ai" else eng.issue_token(intent)
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

        res = self.adapter.apply(intent)
        if not res.get("ok"):
            return self._audit(intent, operator, "refused", res.get("message", "device error"), level)

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

    def rollback(self, token: str) -> bool:
        undo = self.audit.rollback(token)
        if undo is None:
            return False
        self.adapter.undo(undo)
        return True
