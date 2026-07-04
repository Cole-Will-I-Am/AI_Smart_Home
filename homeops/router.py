"""CommandRouter — the one place a command is turned into an action.

Resolves the target house, checks the permission level, applies mode/confirmation/
cross-house/rate-limit/approved-hardware gates, executes via the adapter, and writes an
audit record with a rollback token where the action is reversible. This is enforcement:
the AI can only *propose* intents; it cannot bypass any of these checks.

Part 14: the router also checks SEMANTIC INVARIANTS (permissions.ARG_INVARIANTS) — the
ladder gates verbs, envelopes gate the values. Out-of-envelope arguments escalate to
confirm_required rather than executing silently. Time-dependent envelopes (quiet hours)
read `self.clock`, injectable for tests and deployments (defaults to wall clock).
"""
from __future__ import annotations
from datetime import datetime

from .permissions import (
    PermissionEngine, Intent, Operator, Result, CONFIRM_REQUIRED, SAFETY_CRITICAL, EXPECTED_STATE,
    ROLLBACK_INVERSE, semantic_violation,
)
from .audit import AuditLog, AuditRecord
from .adapters.base import Adapter
from .state import StateStore


class CommandRouter:
    def __init__(self, engine: PermissionEngine, state: StateStore, adapter: Adapter, audit: AuditLog,
                 health=None, clock=None) -> None:
        self.engine = engine
        self.state = state
        self.adapter = adapter
        self.audit = audit
        self.health = health   # optional HealthRegistry; gates safety-critical actuation
        self.clock = clock or datetime.now   # wall clock for time-dependent semantic invariants

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

        # Fail-closed on unknown property identifiers BEFORE any level/confirm logic, so a
        # malformed or hostile house_id can never become a pending confirmation a human might
        # approve (red team: test_unknown_house_*).
        if intent.house_id not in self.state.houses:
            return self._audit(intent, operator, "refused", f"unknown house {intent.house_id!r}", None)

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

        # Part 14 — semantic invariants: the ladder gates verbs; envelopes gate the values.
        # (Adapter-independent: the live HA adapter forwards args raw, so the clamp lives here.)
        violation = semantic_violation(intent, operator, self.clock())

        # confirmation gate
        needs_confirm = (intent.subsystem, intent.action) in CONFIRM_REQUIRED or violation is not None
        if operator.kind == "ai" and level >= 2:
            needs_confirm = True   # AI's L2+ control is conditioned on a human confirmation
        # H4: a token, once validated here, must not be consumed until the action actually
        # actuates — otherwise a later gate (rate/health/cooldown/hardware) refuses and the human's
        # single-use token is silently spent. So we PEEK for authorization and remember to consume
        # at the point of actuation.
        consume_token_at_actuation = False
        if needs_confirm:
            by_emergency = operator.kind == "system" and intent.emergency
            by_token = eng.peek_token(intent, operator)
            consume_token_at_actuation = by_token
            authorized = by_emergency or by_token
            if not authorized:
                # The AI cannot self-confirm — a human must. So it gets no usable token,
                # only the signal that human confirmation is required. Only the OWNER receives
                # a single-use, unguessable token bound to this exact intent (INCLUDING args —
                # a token minted for 200°F cannot be replayed as 205°F) AND to their identity.
                # Guests receive no token either: an out-of-envelope guest request goes to the owner.
                tok = eng.issue_token(intent, operator) if operator.kind == "owner" else None
                why = f"confirmation required for {intent.subsystem}.{intent.action}"
                if violation:
                    why += f" — {violation}"
                return self._audit(intent, operator, "confirm_required", why, level, ctoken=tok)

        # L3 requires approved, professionally-installed hardware
        if level == 3:
            ent = self.state.entity(intent.entity_id)
            if ent is None or not ent.approved_hardware:
                return self._audit(intent, operator, "refused",
                                   "L3 requires approved, professionally-installed hardware", level)

        if not eng.allow_rate(intent):
            return self._audit(intent, operator, "refused", "rate limited", level)

        # Safety-critical health gate: never actuate a lock/valve/generator/HVAC-cutoff we can't
        # confirm is present and responsive.
        key = (intent.subsystem, intent.action)
        if self.health is not None and key in SAFETY_CRITICAL:
            hstatus = self.health.status(intent.entity_id, eng.tick)
            if not self.health.healthy(intent.entity_id, eng.tick):
                return self._audit(intent, operator, "refused",
                                   f"device {intent.entity_id} is {hstatus} — refusing safety-critical actuation", level)

        if not eng.allow_cooldown(intent):
            return self._audit(intent, operator, "refused",
                               f"cooldown: {intent.subsystem}.{intent.action} actuated too recently", level)

        # H4: every refusal gate is now behind us — spend the single-use token at the moment of
        # actuation, not at authorization. A gate above returned before reaching this line, so a
        # refused confirmed action leaves its token intact and re-confirmable.
        if consume_token_at_actuation:
            eng.consume_token(intent)

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
            self.audit.register_rollback(rollback, res["undo"], meta={
                "house_id": intent.house_id, "subsystem": intent.subsystem,
                "target": intent.target, "action": intent.action, "level": level})
        return self._audit(intent, operator, "executed", res["message"], level, rollback=rollback)

    def recommend(self, house_id: str, message: str, operator: Operator, level: int = 4) -> Result:
        return self._audit(
            Intent(house_id=house_id, subsystem="advisory", target="operator", action="recommend"),
            operator, "recommended", message, level,
        )

    def rollback(self, token: str, operator: Operator | None = None) -> bool:
        """Authority-gated undo (review finding R-2). A rollback IS the inverse actuation, so
        it faces the authority of that inverse verb: an operator is required (fail-closed),
        RBAC scope and role caps apply, guests stop at L1, the AI is barred from L2+,
        confirm-required inverses must be re-issued as first-class intents (this bool API
        cannot carry the token dance), safety-critical inverses are health-gated, and the
        rollback token is single-use — consumed before actuation."""
        entry = self.audit.rollback(token)

        def refuse(msg: str, meta: dict | None = None, level=None) -> bool:
            m = meta or {}
            self.audit.record(AuditRecord(
                tick=self.engine.tick, operator=(operator.kind if operator else "unknown"),
                house_id=m.get("house_id", "n/a"), subsystem=m.get("subsystem", "advisory"),
                target=m.get("target", "rollback"), action="rollback",
                args={"token": token}, level=level, status="refused", message=msg))
            return False

        if entry is None:
            return refuse("rollback: unknown token")
        undo, meta = entry["undo"], entry["meta"]
        if operator is None:
            return refuse("rollback requires an authenticated operator")
        orig = (meta.get("subsystem"), meta.get("action"))
        inverse = ROLLBACK_INVERSE.get(orig, orig)
        levels = [lv for lv in (meta.get("level"), self.engine.level(*inverse)) if lv is not None]
        if not levels:
            return refuse("rollback: cannot establish an authority level — failing closed", meta)
        eff = max(levels)
        house = meta.get("house_id", "n/a")
        if operator.houses != "*" and house not in operator.houses:
            return refuse(f"property {house} is out of scope for operator {operator.name}", meta, eff)
        if operator.kind == "guest" and eff > 1:
            return refuse("guest operator limited to Level 1", meta, eff)
        if operator.max_level is not None and eff > operator.max_level:
            return refuse(f"rollback level L{eff} exceeds operator role (max L{operator.max_level})", meta, eff)
        if operator.kind == "ai" and eff >= 2:
            return refuse("AI may not roll back an L2+ action — a human must", meta, eff)
        if inverse in CONFIRM_REQUIRED:
            return refuse(f"rollback would perform {inverse[0]}.{inverse[1]}, which requires "
                          f"confirmation — issue it as a first-class intent", meta, eff)
        entity_id = f"{house}.{meta.get('subsystem')}.{meta.get('target')}"
        if self.health is not None and inverse in SAFETY_CRITICAL \
                and not self.health.healthy(entity_id, self.engine.tick):
            return refuse(f"device {entity_id} is {self.health.status(entity_id, self.engine.tick)} "
                          f"— refusing safety-critical rollback", meta, eff)
        self.audit.consume_rollback(token)   # single-use
        self.adapter.undo(undo)
        self.audit.record(AuditRecord(
            tick=self.engine.tick, operator=operator.kind, house_id=house,
            subsystem=meta.get("subsystem", "advisory"), target=meta.get("target", "rollback"),
            action="rollback", args={"token": token, "inverse": f"{inverse[0]}.{inverse[1]}"},
            level=eff, status="rollback", message=f"rollback applied (= {inverse[0]}.{inverse[1]})"))
        return True
