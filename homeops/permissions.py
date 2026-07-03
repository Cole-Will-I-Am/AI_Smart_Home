"""The 6-level permission model (DESIGN.md §P), enforced server-side.

The level is a property of the *action*, checked here — the AI cannot self-escalate.
L4/L5 have no execution path at all: the router returns `recommend_only` / `prohibited`
and never actuates. Confirmation tokens are single-use, house-scoped, and TTL-bounded.

Part 14 adds SEMANTIC INVARIANTS: the ladder quantifies over verbs; ARG_INVARIANTS
quantifies over the values. See the section below.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import hashlib
import json
import secrets

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

# Minimum ticks between repeats of a one-shot destructive actuation (rolling cooldown,
# independent of the per-tick rate limiter).
DESTRUCTIVE_COOLDOWN: dict[tuple[str, str], int] = {
    ("water", "shutoff_main"): 3,
    ("water", "open_main"): 3,
    ("generator", "start"): 3,
    ("power", "breaker_off"): 3,
}

# Safety-critical actuations: gated on device health (offline/stale -> refuse) and verified by
# read-back afterwards. EXPECTED maps each to the state(s) that count as a confirmed outcome
# (a transitional state like "closing"/"starting" is acceptable — the physical move is underway).
SAFETY_CRITICAL: set[tuple[str, str]] = {
    ("lock", "lock"), ("lock", "unlock"),
    ("water", "shutoff_main"), ("water", "open_main"),
    ("hvac", "emergency_shutoff"), ("generator", "start"),
}
EXPECTED_STATE: dict[tuple[str, str], set] = {
    ("lock", "lock"): {"locked"}, ("lock", "unlock"): {"unlocked"},
    ("water", "shutoff_main"): {"closed", "closing"}, ("water", "open_main"): {"open"},
    ("hvac", "emergency_shutoff"): {"off"},
    ("generator", "start"): {"starting", "running"},
}

# --- Part 14: semantic invariants — envelopes over ARGUMENTS, not just verbs ---------------
# Review finding R-1: ACTION_LEVELS quantifies over (subsystem, action), but a complete speech
# act is verb + arguments + context. The simulator clamps a 200°F setpoint; the live HA adapter
# forwards it raw — so the sim was flattering the engine by enforcing semantics at the wrong
# layer. The envelope therefore lives HERE, in the authority layer, adapter-independent.
#
# An out-of-envelope argument never silently executes: the router escalates it to
# confirm_required. The token an OWNER then receives is bound to those exact args, so the
# override is explicit, single-use, and audited. Guests and the AI receive no token.
# `system` operators (local automations, running below the AI with reviewed hard-coded values)
# are exempt — an emergency response must never be blockable by a comfort envelope.

# Rolling back an action is semantically PERFORMING its inverse verb, so the router gates a
# rollback at the authority of that inverse (review finding R-2). Actions not listed here are
# pure state restores and gate at their own level.
ROLLBACK_INVERSE: dict[tuple[str, str], tuple[str, str]] = {
    ("light", "turn_on"): ("light", "turn_off"), ("light", "turn_off"): ("light", "turn_on"),
    ("plug", "turn_on"): ("plug", "turn_off"), ("plug", "turn_off"): ("plug", "turn_on"),
    ("cover", "open"): ("cover", "close"), ("cover", "close"): ("cover", "open"),
    ("lock", "lock"): ("lock", "unlock"), ("lock", "unlock"): ("lock", "lock"),
    ("garage", "open"): ("garage", "close"), ("garage", "close"): ("garage", "open"),
    ("alarm", "arm"): ("alarm", "disarm"), ("alarm", "disarm"): ("alarm", "arm"),
    ("water", "shutoff_main"): ("water", "open_main"), ("water", "open_main"): ("water", "shutoff_main"),
    ("water", "irrigation_on"): ("water", "irrigation_off"), ("water", "irrigation_off"): ("water", "irrigation_on"),
}

QUIET_HOURS = (22, 7)   # announcements in 22:00–06:59 require a human


def _within(args: dict, key: str, lo: float, hi: float) -> str | None:
    if key not in args or args.get(key) is None:
        return None                       # absent -> adapter default; nothing to judge
    try:
        v = float(args[key])
    except (TypeError, ValueError):
        return f"{key}={args.get(key)!r} is not numeric"
    return None if lo <= v <= hi else f"{key}={v:g} outside envelope [{lo:g}, {hi:g}]"


def _quiet(args: dict, now) -> str | None:
    start, end = QUIET_HOURS
    h = now.hour
    in_quiet = (h >= start or h < end) if start > end else (start <= h < end)
    return (f"quiet hours {start:02d}:00–{end - 1:02d}:59: announce at {h:02d}:xx requires a human"
            if in_quiet else None)


# (subsystem, action) -> callable(args, now) returning a violation reason, or None if fine.
ARG_INVARIANTS: dict[tuple[str, str], Any] = {
    ("climate", "set_temperature"): lambda a, now: _within(a, "temperature", 50, 90),
    ("evcharger", "set_limit"):     lambda a, now: _within(a, "amps", 6, 48),
    ("speaker", "announce"):        _quiet,
}


def semantic_violation(intent: "Intent", operator: "Operator", now) -> str | None:
    """Return a human-readable envelope violation, or None. System operators are exempt."""
    if operator.kind == "system":
        return None
    inv = ARG_INVARIANTS.get((intent.subsystem, intent.action))
    return inv(intent.args, now) if inv else None


@dataclass
class Operator:
    kind: str            # owner | ai | system | guest
    active_house: str
    name: str = ""
    max_level: int | None = None   # role cap (None = uncapped); set via identity.operator_for
    houses: object = "*"           # property scope: "*" or a set of house ids this operator may touch


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
        self._tokens: dict[str, tuple[str, int]] = {}   # token -> (intent+operator key, expiry_tick)
        self._rate_limit = rate_limit
        self._rate: dict[tuple[str, str], tuple[int, int]] = {}   # (house, subsystem) -> (tick, count)
        self._last_action: dict[tuple[str, str, str, str], int] = {}   # cooldown tracker
        self.tick = 0

    def level(self, subsystem: str, action: str) -> int | None:
        return ACTION_LEVELS.get((subsystem, action))

    # --- confirmation tokens (cryptographic; bound to full intent + operator) ---
    def _key(self, intent: Intent, operator: "Operator") -> str:
        payload = {
            "house": intent.house_id, "subsystem": intent.subsystem, "target": intent.target,
            "action": intent.action, "args": intent.args,
            "op_kind": operator.kind, "op_name": operator.name, "op_house": operator.active_house,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def issue_token(self, intent: Intent, operator: "Operator", ttl: int = 5) -> str:
        # hygiene: sweep expired, never-consumed tokens so the table cannot grow without bound
        if self._tokens:
            self._tokens = {t: ke for t, ke in self._tokens.items() if ke[1] >= self.tick}
        tok = secrets.token_urlsafe(16)   # unguessable
        self._tokens[tok] = (self._key(intent, operator), self.tick + ttl)
        return tok

    def check_token(self, intent: Intent, operator: "Operator") -> bool:
        tok = intent.confirm_token
        if not tok or tok not in self._tokens:
            return False
        key, expiry = self._tokens[tok]
        # bound to the EXACT intent (incl. args) AND the same operator, and not expired
        if key != self._key(intent, operator) or self.tick > expiry:
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

    def allow_cooldown(self, intent: Intent) -> bool:
        cd = DESTRUCTIVE_COOLDOWN.get((intent.subsystem, intent.action))
        if not cd:
            return True
        k = (intent.house_id, intent.subsystem, intent.target, intent.action)
        last = self._last_action.get(k)
        if last is not None and self.tick - last < cd:
            return False
        self._last_action[k] = self.tick
        return True
