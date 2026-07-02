"""The 6-level permission model (DESIGN.md §P), enforced server-side.

The level is a property of the *action*, checked here — the AI cannot self-escalate.
L4/L5 have no execution path at all: the router returns `recommend_only` / `prohibited`
and never actuates. Confirmation tokens are single-use, house-scoped, and TTL-bounded.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

# (subsystem, action) -> level.  0 Observe · 1 Routine · 2 Security/Utility · 3 Power/Infra · 4 Recommend-only · 5 Prohibited
ACTION_LEVELS: dict[tuple[str, str], int] = {
    ("light", "turn_on"): 1, ("light", "turn_off"): 1, ("light", "set_brightness"): 1,
    ("climate", "set_temperature"): 1, ("climate", "set_fan"): 1, ("climate", "set_mode"): 3,
    ("cover", "open"): 1, ("cover", "close"): 1, ("cover", "set_position"): 1,
    ("plug", "turn_on"): 1, ("plug", "turn_off"): 1,
    ("speaker", "announce"): 1,
    ("scene", "activate"): 1, ("notify", "send"): 1,
    ("lock", "lock"): 2, ("lock", "unlock"): 2,
    ("alarm", "arm"): 2, ("alarm", "disarm"): 2, ("alarm", "escalate"): 2,
    ("garage", "close"): 2, ("garage", "open"): 2,
    ("camera", "set_mode"): 2, ("camera", "snapshot"): 2, ("camera", "export"): 2,
    ("water", "irrigation_on"): 2, ("water", "irrigation_off"): 2,
    ("network", "quarantine"): 2,
    ("water", "shutoff_main"): 3, ("water", "open_main"): 3,
    ("power", "breaker_on"): 3, ("power", "breaker_off"): 3, ("power", "load_shed"): 3,
    ("generator", "start"): 3, ("battery", "set_mode"): 3, ("evcharger", "set_limit"): 3,
    ("hvac", "emergency_shutoff"): 3, ("network", "firewall_policy"): 3,
    # L4 — recommend only
    ("power", "main_breaker"): 4, ("network", "firewall_restructure"): 4,
    ("lock", "unlock_unknown"): 4, ("alarm", "disable"): 4, ("utility", "change"): 4,
    # L5 — prohibited
    ("safety", "bypass"): 5, ("alarm", "disable_smoke_co"): 5, ("meter", "tamper"): 5,
}

# Actions that require an explicit human confirmation token even for the owner.
CONFIRM_REQUIRED: set[tuple[str, str]] = {
    ("lock", "unlock"), ("alarm", "disarm"), ("garage", "open"),
    ("water", "shutoff_main"), ("power", "breaker_off"), ("generator", "start"),
    ("network", "firewall_policy"),
}


@dataclass
class Operator:
    kind: str            # owner | ai | system | guest
    active_house: str
    name: str = ""


@dataclass
class Intent:
    house_id: str
    subsystem: str
    target: str
    action: str
    args: dict = field(default_factory=dict)
    confirm_token: str | None = None
    confirm_cross_house: bool = False
    emergency: bool = False   # set only by local automations for pre-authorized emergency responses

    @property
    def entity_id(self) -> str:
        return f"{self.house_id}.{self.subsystem}.{self.target}"


@dataclass
class Result:
    status: str            # executed | confirm_required | refused | prohibited | recommend_only | recommended
    message: str
    level: int | None = None
    confirm_token: str | None = None
    rollback_token: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "executed"


class PermissionEngine:
    def __init__(self, rate_limit: int = 5) -> None:
        self._tokens: dict[str, tuple[str, int]] = {}   # token -> (intent-key, expiry_tick)
        self._counter = 0
        self._rate_limit = rate_limit
        self._rate: dict[tuple[str, str], tuple[int, int]] = {}   # (house, subsystem) -> (tick, count)
        self.tick = 0

    def level(self, subsystem: str, action: str) -> int | None:
        return ACTION_LEVELS.get((subsystem, action))

    # --- confirmation tokens -------------------------------------------------
    def _key(self, intent: Intent) -> str:
        return f"{intent.house_id}|{intent.subsystem}|{intent.target}|{intent.action}"

    def issue_token(self, intent: Intent, ttl: int = 5) -> str:
        self._counter += 1
        tok = f"tok-{self._counter}"
        self._tokens[tok] = (self._key(intent), self.tick + ttl)
        return tok

    def check_token(self, intent: Intent) -> bool:
        tok = intent.confirm_token
        if not tok or tok not in self._tokens:
            return False
        key, expiry = self._tokens[tok]
        if key != self._key(intent) or self.tick > expiry:
            return False
        del self._tokens[tok]   # single-use
        return True

    # --- rate limiting -------------------------------------------------------
    def allow_rate(self, intent: Intent) -> bool:
        k = (intent.house_id, intent.subsystem)
        tick, count = self._rate.get(k, (self.tick, 0))
        if tick != self.tick:
            count = 0
        if count >= self._rate_limit:
            return False
        self._rate[k] = (self.tick, count + 1)
        return True
