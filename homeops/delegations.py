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
  * only L1/L2 actions are delegable: L3 (breakers, generator, mains) stays per-act,
    L4/L5 remain execution-path-free — enforced at grant time AND re-checked at match
    time, fail-closed, in case ACTION_LEVELS ever changes,
  * semantic invariants (Part 14) outrank standing consent: a delegation can never
    standing-approve an out-of-envelope value,
  * every delegated execution writes a paired, hash-chained `delegated` advisory record
    naming its certificate; any non-executed outcome (health gate, cooldown, rate limit,
    unverified read-back) falls back to the ordinary pending-confirmation path and does
    not consume budget.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime

from .audit import AuditRecord
from .permissions import ACTION_LEVELS, Intent, Operator, semantic_violation

DELEGABLE_MAX_LEVEL = 2


@dataclass
class Delegation:
    id: str
    grantor: str                        # the human principal who signed this standing consent
    house_id: str
    subsystem: str
    action: str
    window: tuple[int, int] | None = None       # (start_hour, end_hour) inclusive; wraps midnight if start > end
    args_within: dict | None = None             # declarative arg envelope: key -> (lo, hi)
    budget_per_day: int = 4
    expires: date | None = None
    revoked: bool = False
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
        if (intent.house_id, intent.subsystem, intent.action) != (self.house_id, self.subsystem, self.action):
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
                "revoked": self.revoked}

    @classmethod
    def from_dict(cls, d: dict) -> "Delegation":
        return cls(id=d["id"], grantor=d["grantor"], house_id=d["house_id"],
                   subsystem=d["subsystem"], action=d["action"],
                   window=tuple(d["window"]) if d.get("window") else None,
                   args_within={k: tuple(v) for k, v in d["args_within"].items()} if d.get("args_within") else None,
                   budget_per_day=d.get("budget_per_day", 4),
                   expires=date.fromisoformat(d["expires"]) if d.get("expires") else None,
                   revoked=bool(d.get("revoked", False)))


class DelegationRegistry:
    """Holds the estate's standing-consent certificates. Clock is injectable for tests."""

    def __init__(self, clock=None) -> None:
        self.clock = clock or datetime.now
        self._delegations: dict[str, Delegation] = {}

    def grant(self, d: Delegation) -> Delegation:
        lvl = ACTION_LEVELS.get((d.subsystem, d.action))
        if lvl is None:
            raise ValueError(f"cannot delegate unknown action {d.subsystem}.{d.action}")
        if lvl > DELEGABLE_MAX_LEVEL:
            raise ValueError(
                f"L{lvl} is not delegable — standing consent stops at L{DELEGABLE_MAX_LEVEL}")
        self._delegations[d.id] = d
        return d

    def revoke(self, did: str) -> bool:
        d = self._delegations.get(did)
        if d is not None:
            d.revoked = True
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
    res = world.router.execute(intent, grantor_op)
    intent.confirm_token = None
    if not res.ok:
        return None, None
    d.used_today += 1
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
