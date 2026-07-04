"""Part 15 — delegation certificates: bounded standing consent, engine-side.

Every L2 action interrupts the resident; that vigilance tax is the cost the product exists
to remove. A Delegation is a standing policy the owner grants once — (house, subsystem,
action) within a time window, optionally an argument envelope, with a daily budget, an
expiry, and revocation — that lets the ENGINE perform the confirm dance on the grantor's
behalf when the AI proposes a matching action. Trust accrues to the deterministic policy,
never to the model:

  * the token is minted and consumed inside `try_delegated_execute` under an owner-kind
    operator named for the certificate — it never enters the model's context
    (regression-tested in tests/test_delegations.py),
  * only explicitly delegable L1/L2/L3 actions are covered. Reversible L3 power/infra
    actions (battery modes, EV limits, load-shed, breaker-on, climate mode) may be covered,
    but life-safety-adjacent or one-shot destructive actions remain per-act: anything in
    SAFETY_CRITICAL or DESTRUCTIVE_COOLDOWN is non-delegable. L4/L5 remain execution-path-free.
    This rule is enforced at grant time AND re-checked at match time, fail-closed, in case
    ACTION_LEVELS ever changes,
  * semantic invariants (Part 14) outrank standing consent: a delegation can never
    standing-approve an out-of-envelope value,
  * every delegated execution writes a paired, hash-chained `delegated` advisory record
    naming its certificate; any non-executed outcome (health gate, cooldown, rate limit,
    unverified read-back) falls back to the ordinary pending-confirmation path and does
    not consume budget.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import uuid4

from .audit import AuditRecord
from .permissions import (
    ACTION_LEVELS, DESTRUCTIVE_COOLDOWN, SAFETY_CRITICAL, Intent, Operator, semantic_violation,
)

DELEGABLE_MAX_LEVEL = 3


def delegation_block_reason(subsystem: str, action: str) -> str | None:
    """Return why an action is not eligible for standing consent, or None if it is.

    Delegation is intentionally narrower than execution: the router may execute L3 after a
    human confirmation, but standing authority covers only reversible/non-life-safety actions.
    """
    lvl = ACTION_LEVELS.get((subsystem, action))
    if lvl is None:
        return f"cannot delegate unknown action {subsystem}.{action}"
    if lvl > DELEGABLE_MAX_LEVEL:
        return f"L{lvl} is not delegable — standing consent stops at L{DELEGABLE_MAX_LEVEL}"
    if (subsystem, action) in SAFETY_CRITICAL:
        return f"{subsystem}.{action} is safety-critical and remains per-act"
    if (subsystem, action) in DESTRUCTIVE_COOLDOWN:
        return f"{subsystem}.{action} is destructive/cooldown-gated and remains per-act"
    return None


def is_delegable_action(subsystem: str, action: str) -> bool:
    return delegation_block_reason(subsystem, action) is None


@dataclass
class Delegation:
    id: str
    grantor: str                        # the human principal who signed this standing consent
    house_id: str
    subsystem: str                       # exact subsystem, or "*" for standing authority
    action: str                          # exact action, or "*" for standing authority
    window: tuple[int, int] | None = None       # (start_hour, end_hour) inclusive; wraps midnight if start > end
    args_within: dict | None = None             # declarative arg envelope: key -> (lo, hi)
    budget_per_day: int = 200
    expires: date | None = None
    revoked: bool = False
    max_level: int | None = None                # only for wildcard standing-authority certs
    used_today: int = 0
    _day: date | None = field(default=None, repr=False)

    def _roll(self, now: datetime) -> None:
        if self._day != now.date():
            self._day, self.used_today = now.date(), 0

    def matches(self, intent: Intent, now: datetime) -> bool:
        if self.revoked or (self.expires is not None and now.date() > self.expires):
            return False
        lvl = ACTION_LEVELS.get((intent.subsystem, intent.action))
        if lvl is None or lvl > DELEGABLE_MAX_LEVEL:   # fail-closed, re-checked at match time
            return False
        if not is_delegable_action(intent.subsystem, intent.action):
            return False
        if intent.house_id != self.house_id:
            return False
        if self.subsystem == "*" and self.action == "*":
            if self.max_level is None or lvl > self.max_level:
                return False
        elif (intent.subsystem, intent.action) != (self.subsystem, self.action):
            return False
        if self.window is not None:
            s, e = self.window
            h = now.hour
            if not ((h >= s or h <= e) if s > e else (s <= h <= e)):
                return False
        if self.args_within:
            for k, (lo, hi) in self.args_within.items():
                v = intent.args.get(k)
                try:
                    if v is None or not (lo <= float(v) <= hi):
                        return False
                except (TypeError, ValueError):
                    return False
        self._roll(now)
        return self.used_today < self.budget_per_day

    # --- serialization (for a future deployment-descriptor section) -------------------
    def to_dict(self) -> dict:
        return {"id": self.id, "grantor": self.grantor, "house_id": self.house_id,
                "subsystem": self.subsystem, "action": self.action,
                "window": list(self.window) if self.window else None,
                "args_within": {k: list(v) for k, v in self.args_within.items()} if self.args_within else None,
                "budget_per_day": self.budget_per_day,
                "expires": self.expires.isoformat() if self.expires else None,
                "revoked": self.revoked,
                "max_level": self.max_level,
                "used_today": self.used_today,
                "day": self._day.isoformat() if self._day else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Delegation":
        return cls(id=d["id"], grantor=d["grantor"], house_id=d["house_id"],
                   subsystem=d["subsystem"], action=d["action"],
                   window=tuple(d["window"]) if d.get("window") else None,
                   args_within={k: tuple(v) for k, v in d["args_within"].items()} if d.get("args_within") else None,
                   budget_per_day=d.get("budget_per_day", 200),
                   expires=date.fromisoformat(d["expires"]) if d.get("expires") else None,
                   revoked=bool(d.get("revoked", False)),
                   max_level=d.get("max_level"),
                   used_today=int(d.get("used_today", 0)),
                   _day=date.fromisoformat(d["day"]) if d.get("day") else None)


class DelegationRegistry:
    """Holds the estate's standing-consent certificates. Clock is injectable for tests."""

    def __init__(self, clock=None, path=None) -> None:
        self.clock = clock or datetime.now
        self._delegations: dict[str, Delegation] = {}
        self._path = path                      # R6: None = in-memory; a path persists standing consent
        if path and os.path.exists(path):
            self._load()

    # --- R6: durable standing consent across restart -------------------------------------
    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([d.to_dict() for d in self._delegations.values()], f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)   # atomic — a crash never leaves a half-written registry

    def _load(self) -> None:
        with open(self._path) as f:
            for e in json.load(f):
                d = Delegation.from_dict(e)
                self._delegations[d.id] = d

    def persist(self) -> None:
        """R6: record a mutation made outside grant/revoke (e.g. a consumed daily budget)."""
        self._save()

    def grant(self, d: Delegation, by: Operator) -> Delegation:
        """Admit a certificate — after checking BOTH the action (delegable level) and the
        GRANTOR's authority (review finding R-3): only an owner whose property scope covers
        the house and whose role cap admits the action may mint standing consent. Without
        this, whoever can call grant() mints owner-level authority."""
        if by is None or getattr(by, "kind", None) != "owner":
            raise PermissionError(
                f"only an owner may grant standing consent (grantor kind={getattr(by, 'kind', None)!r})")
        if by.houses != "*" and d.house_id not in by.houses:
            raise PermissionError(f"grantor {by.name or 'owner'!r} has no authority over {d.house_id}")
        wildcard = d.subsystem == "*" and d.action == "*"
        if wildcard:
            lvl = d.max_level
            if lvl is None:
                raise ValueError("standing authority requires max_level")
            if lvl > DELEGABLE_MAX_LEVEL:
                raise ValueError(
                    f"L{lvl} is not delegable — standing consent stops at L{DELEGABLE_MAX_LEVEL}")
            if lvl < 0:
                raise ValueError("standing authority max_level must be non-negative")
        else:
            lvl = ACTION_LEVELS.get((d.subsystem, d.action))
            reason = delegation_block_reason(d.subsystem, d.action)
            if reason is not None:
                raise ValueError(reason)
        if by.max_level is not None and lvl > by.max_level:
            raise PermissionError(f"grantor role caps at L{by.max_level}; cannot delegate an L{lvl} action")
        self._delegations[d.id] = d
        self._save()
        return d

    def revoke(self, did: str) -> bool:
        d = self._delegations.get(did)
        if d is not None:
            d.revoked = True
            self._save()
        return d is not None

    def match(self, intent: Intent) -> Delegation | None:
        now = self.clock()
        for d in self._delegations.values():
            if d.matches(intent, now):
                return d
        return None

    def __iter__(self):
        return iter(self._delegations.values())

    def __len__(self) -> int:
        return len(self._delegations)


def try_delegated_execute(world, intent: Intent, registry: DelegationRegistry):
    """(Result, Delegation) if a standing certificate covered AND executed the intent, else (None, None).

    The engine — never the model — mints and consumes the confirmation token, under an
    owner-kind operator named for the certificate. Semantic invariants are re-checked first,
    so a delegation cannot standing-approve an out-of-envelope value. Non-executed outcomes
    leave the intent on the ordinary confirm_required path and consume no budget.
    """
    d = registry.match(intent)
    if d is None:
        return None, None
    grantor_op = Operator(kind="owner", active_house=intent.house_id,
                          name=f"delegation:{d.id}:{d.grantor}")
    if semantic_violation(intent, grantor_op, registry.clock()) is not None:
        return None, None      # Part 14 envelopes outrank standing consent
    eng = world.router.engine
    intent.confirm_token = eng.issue_token(intent, grantor_op)
    try:
        res = world.router.execute(intent, grantor_op)
    finally:
        # A delegated token is engine-internal. If a downstream gate refuses before the router
        # consumes it, do not leave that token alive; fall back to the ordinary pending path.
        eng.consume_token(intent)
        intent.confirm_token = None
    if not res.ok:
        return None, None
    d.used_today += 1
    registry.persist()   # R6: a consumed daily budget must survive a restart
    world.router.audit.record(AuditRecord(
        tick=eng.tick, operator="owner", house_id=intent.house_id,
        subsystem="advisory", target=d.id, action="delegation_used",
        args={"grantor": d.grantor,
              "covered": f"{intent.subsystem}.{intent.target}.{intent.action}",
              "args": dict(intent.args),
              "used_today": d.used_today, "budget": d.budget_per_day},
        level=res.level, status="delegated",
        message=f"standing delegation {d.id} executed {intent.subsystem}.{intent.action} for {d.grantor}",
    ))
    return res, d


def grant_standing_authority(grantor: Operator, house_id: str, max_level: int,
                             window: tuple[int, int] | None = None, budget: int = 200,
                             expiry: date | None = None,
                             args_within: dict | None = None,
                             registry: DelegationRegistry | None = None) -> Delegation:
    """Human owner helper for broad standing authority over delegable actions up to max_level.

    This is deliberately not an AI tool. The caller supplies the authenticated human owner
    (`grantor`); `DelegationRegistry.grant()` still performs owner/scope/role-cap checks.
    """
    if grantor is None or getattr(grantor, "kind", None) != "owner":
        raise PermissionError("only an owner may grant standing authority")
    if max_level > DELEGABLE_MAX_LEVEL:
        raise ValueError(f"standing authority max_level must be <= {DELEGABLE_MAX_LEVEL}")
    reg = registry if registry is not None else DelegationRegistry()
    name = grantor.name or "owner"
    d = Delegation(
        id=f"sa-{house_id}-L{max_level}-{uuid4().hex[:8]}",
        grantor=name,
        house_id=house_id,
        subsystem="*",
        action="*",
        window=window,
        args_within=args_within,
        budget_per_day=budget,
        expires=expiry,
        max_level=max_level,
    )
    return reg.grant(d, grantor)
