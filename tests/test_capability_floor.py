"""Capability floor / safety ceiling.

After the deliberate relaxation of nuisance guards (rate/L1/delegation budgets) and
perception throttles (snapshot, tool-turns, history), this file pins BOTH edges:

  * the capability FLOOR — the guards are generous, not stingy (so nobody silently
    re-tightens a nuisance guard under the mistaken belief it is a safety net);
  * the safety CEILING — every outcome-gating mechanism is UNCHANGED (so nobody
    silently loosens a real safety net under the mistaken belief it is a nuisance guard).

The distinction is the whole thesis: a safety net bounds what is *permitted*; a nuisance
guard bounds how *much* of the permitted you may do. Only the latter was relaxed.
"""
from datetime import datetime

from homeops.permissions import (
    PermissionEngine, Intent, Operator, ACTION_LEVELS, DESTRUCTIVE_COOLDOWN,
    requires_confirmation, semantic_violation,
)
from homeops.delegations import is_delegable_action, DELEGABLE_MAX_LEVEL, Delegation
import homeops.ai.session as session_mod


# --- capability FLOOR: nuisance/runaway guards are generous ------------------------------

def test_rate_guard_is_generous_enough_for_a_scene():
    eng = PermissionEngine()
    lit = sum(eng.allow_rate(Intent("house_a", "light", f"l{i}", "turn_on")) for i in range(40))
    assert eng._rate_limit == 50 and lit >= 40   # a 40-light scene no longer clips at 5


def test_daily_budgets_are_generous():
    assert PermissionEngine()._ai_l1_budget == 600
    assert Delegation("x", "o", "house_a", "*", "*", max_level=3).budget_per_day == 200


def test_agentic_depth_is_deep():
    d = session_mod.ChatSession.__init__.__defaults__
    assert 16 in d and 30 in d   # max_tool_turns=16, max_history_turns=30


# --- safety CEILING: every outcome-gating mechanism is UNCHANGED -------------------------

def test_ladder_unchanged():
    assert ACTION_LEVELS[("safety", "bypass")] == 5
    assert ACTION_LEVELS[("power", "main_breaker")] == 4
    assert ACTION_LEVELS[("generator", "start")] == 3


def test_confirmation_gating_unchanged():
    ai = Operator("ai", "house_a")
    owner = Operator("owner", "house_a")
    assert requires_confirmation(Intent("house_a", "battery", "m", "set_mode", {"mode": "backup"}), ai, 3) is True
    assert requires_confirmation(Intent("house_a", "lock", "f", "unlock"), owner, 2) is True


def test_semantic_envelopes_unchanged():
    now = datetime(2025, 1, 1, 12, 0)
    ai = Operator("ai", "house_a")
    assert semantic_violation(Intent("house_a", "climate", "z", "set_temperature", {"temperature": 200}), ai, now) is not None
    assert semantic_violation(Intent("house_a", "evcharger", "m", "set_limit", {"amps": 3}), ai, now) is not None
    assert semantic_violation(Intent("house_a", "evcharger", "m", "set_limit", {"amps": 0}), ai, now) is None


def test_destructive_cooldown_unchanged():
    assert DESTRUCTIVE_COOLDOWN.get(("generator", "start")) == 3
    assert DESTRUCTIVE_COOLDOWN.get(("water", "shutoff_main")) == 3


def test_delegation_exclusions_unchanged():
    assert DELEGABLE_MAX_LEVEL == 3
    for s, a in [("generator", "start"), ("water", "shutoff_main"), ("power", "breaker_off"),
                 ("hvac", "emergency_shutoff"), ("lock", "unlock"), ("power", "main_breaker")]:
        assert is_delegable_action(s, a) is False
    for s, a in [("battery", "set_mode"), ("evcharger", "set_limit"), ("climate", "set_mode")]:
        assert is_delegable_action(s, a) is True
