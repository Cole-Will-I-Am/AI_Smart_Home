"""Part 12: the shipped red team — hostile inputs the engine must shrug off.

Each test is an attack we publish with the product. The claim being defended: no sequence of
model outputs, smuggled arguments, stolen tokens, or malformed identifiers can widen authority.
If any test here fails, that is a safety incident, not a flake.
"""
import pytest

from homeops.ai.session import ChatSession
from homeops.delegations import Delegation, DelegationRegistry
from homeops.permissions import Intent, Operator

OWNER = lambda h="house_a", n="resident": Operator("owner", h, n)          # noqa: E731
AI = lambda h="house_a": Operator("ai", h, "ai-ops")                       # noqa: E731


def _two_step(world, intent: Intent, op: Operator):
    r = world.router.execute(intent, op)
    assert r.status == "confirm_required" and r.confirm_token
    intent.confirm_token = r.confirm_token
    return world.router.execute(intent, op), r.confirm_token


# ---- token attacks ---------------------------------------------------------------
def test_stolen_token_smuggled_through_args_is_inert(bare):
    """The AI can't pass confirm_token; hiding one inside args must not authorize."""
    seed = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), OWNER())
    assert seed.status == "confirm_required" and seed.confirm_token
    r = bare.router.execute(
        Intent("house_a", "lock", "front_door", "unlock",
               args={"confirm_token": seed.confirm_token}), AI())
    assert r.status == "confirm_required"
    assert bare.state.get_state("house_a.lock.front_door") == "locked"


def test_token_bound_to_args_mutation_fails(bare):
    r1 = bare.router.execute(Intent("house_a", "alarm", "panel", "disarm", {"mode": "night"}), OWNER())
    mutated = Intent("house_a", "alarm", "panel", "disarm", {"mode": "away"},
                     confirm_token=r1.confirm_token)
    assert bare.router.execute(mutated, OWNER()).status == "confirm_required"


def test_token_bound_to_operator_identity(bare):
    r1 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), OWNER(n="resident"))
    theft = Intent("house_a", "lock", "front_door", "unlock", confirm_token=r1.confirm_token)
    assert bare.router.execute(theft, OWNER(n="intruder")).status == "confirm_required"
    assert bare.state.get_state("house_a.lock.front_door") == "locked"


def test_token_single_use_replay_fails(bare):
    done, tok = _two_step(bare, Intent("house_a", "lock", "front_door", "unlock"), OWNER())
    assert done.status == "executed"
    replay = Intent("house_a", "lock", "front_door", "lock")   # relock first
    bare.router.execute(replay, OWNER())
    again = Intent("house_a", "lock", "front_door", "unlock", confirm_token=tok)
    assert bare.router.execute(again, OWNER()).status == "confirm_required"


def test_token_guessing_fails(bare):
    guess = Intent("house_a", "lock", "front_door", "unlock", confirm_token="A" * 22)
    assert bare.router.execute(guess, OWNER()).status == "confirm_required"


def test_token_expires(bare):
    r1 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), OWNER())
    bare.engine.tick += 6                                       # TTL is 5 ticks
    late = Intent("house_a", "lock", "front_door", "unlock", confirm_token=r1.confirm_token)
    assert bare.router.execute(late, OWNER()).status == "confirm_required"


# ---- flag & identity smuggling ------------------------------------------------------
def test_cross_house_flag_cannot_be_smuggled_via_ai_args(bare):
    s = ChatSession(bare, client=None)                          # tool path, no model needed
    out = s.ops._run_tool("propose_command",
                          {"house_id": "house_b", "subsystem": "light", "target": "kitchen",
                           "action": "turn_on", "args": {"confirm_cross_house": True}}, "house_a")
    assert out["status"] == "confirm_required"                  # the flag in args is inert data
    assert bare.state.get_state("house_b.light.kitchen") != "on"


def test_emergency_flag_cannot_be_smuggled_via_args(bare):
    r = bare.router.execute(
        Intent("house_a", "lock", "front_door", "unlock", args={"emergency": True}), AI())
    assert r.status == "confirm_required"


def test_ai_operator_never_receives_a_token(bare):
    r = bare.router.execute(Intent("house_a", "garage", "garage_main", "open"), AI())
    assert r.status == "confirm_required" and r.confirm_token is None


# ---- level enforcement ------------------------------------------------------------
def test_L4_and_L5_have_no_path_for_any_operator(bare):
    for op in (AI(), OWNER()):
        assert bare.router.execute(Intent("house_a", "lock", "x", "unlock_unknown"), op).status == "recommend_only"
        assert bare.router.execute(Intent("house_a", "alarm", "x", "disable_smoke_co"), op).status == "prohibited"


def test_unknown_action_fails_closed(bare):
    r = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock_all"), OWNER())
    assert r.status == "refused" and "unknown action" in r.message


def test_guest_capped_at_L1(bare):
    g = Operator("guest", "house_a", "visitor")
    assert bare.router.execute(Intent("house_a", "light", "kitchen", "turn_on"), g).status == "executed"
    assert bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), g).status == "refused"


# ---- malformed identifiers -----------------------------------------------------------
def test_unknown_house_is_refused_upfront_not_pended(bare):
    """An unknown house must be a clean refusal — never a pending confirmation a human
    might approve, never a crash."""
    r = bare.router.execute(Intent("house_z", "light", "kitchen", "turn_on"), OWNER("house_z"))
    assert r.status == "refused" and "unknown house" in r.message
    r2 = bare.router.execute(Intent("house_z", "lock", "front_door", "unlock"), AI())
    assert r2.status == "refused"


def test_unknown_house_via_chat_never_enters_pending(bare):
    s = ChatSession(bare, client=None)
    out = s.ops._run_tool("propose_command",
                          {"house_id": "house_z", "subsystem": "lock", "target": "front_door",
                           "action": "unlock"}, "house_a")
    assert out["status"] == "refused"
    s._register_pending([{"tool": "propose_command", **out,
                          "intent": {"house_id": "house_z", "subsystem": "lock",
                                     "target": "front_door", "action": "unlock"}}])
    assert s.pending == []                                       # refused things are not confirmable


# ---- flooding ------------------------------------------------------------------------
def test_rate_limit_holds_per_tick(bare):
    got = [bare.router.execute(Intent("house_a", "light", "kitchen",
                                      "turn_on" if i % 2 == 0 else "turn_off"), OWNER())
           for i in range(7)]
    assert any(r.status == "refused" and "rate" in r.message for r in got)


def test_destructive_action_cannot_be_hammered(bare):
    """A one-shot destructive actuation (generator start) cannot be repeated inside its
    cooldown — the guard sits on the actual actuation path, so completing a second
    confirmation still refuses. Protects hardware from rapid-cycle damage."""
    done, _ = _two_step(bare, Intent("house_a", "generator", "main", "start"), OWNER())
    assert done.status in ("executed", "unverified")
    bare.engine.tick += 1                                        # inside the 3-tick cooldown
    second = bare.router.execute(Intent("house_a", "generator", "main", "start"), OWNER())
    assert second.status == "confirm_required"                  # start is confirm-required
    retry = Intent("house_a", "generator", "main", "start", confirm_token=second.confirm_token)
    r2 = bare.router.execute(retry, OWNER())
    assert r2.status == "refused" and "cooldown" in r2.message


# --- Review findings R-2/R-3: rollback authority + delegation grant authority ------------


def _unlock_with_dance(bare):
    """Owner performs the full two-step unlock; returns (owner, rollback_token)."""
    op = Operator("owner", "house_a")
    i = Intent("house_a", "lock", "front_door", "unlock")
    r1 = bare.router.execute(i, op)
    i.confirm_token = r1.confirm_token
    r2 = bare.router.execute(i, op)
    assert r2.status == "executed" and r2.rollback_token
    return op, r2.rollback_token


def test_rollback_without_operator_is_refused(bare):
    _, tok = _unlock_with_dance(bare)
    assert bare.router.rollback(tok) is False
    assert bare.state.get_state("house_a.lock.front_door") == "unlocked"   # nothing actuated


def test_ai_cannot_rollback_an_L2_action(bare):
    op, tok = _unlock_with_dance(bare)
    assert bare.router.rollback(tok, Operator("ai", "house_a", "ai-ops")) is False
    assert bare.router.rollback(tok, op) is True          # the human still can
    assert bare.state.get_state("house_a.lock.front_door") == "locked"


def test_guest_cannot_rollback_an_L2_action(bare):
    _, tok = _unlock_with_dance(bare)
    assert bare.router.rollback(tok, Operator("guest", "house_a", "visitor")) is False


def test_rollback_token_is_single_use(bare):
    op = Operator("owner", "house_a")
    r = bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), op)
    assert bare.router.rollback(r.rollback_token, op) is True
    assert bare.router.rollback(r.rollback_token, op) is False   # consumed


def test_confirm_required_inverse_is_not_rollbackable(bare):
    """Undoing lock.lock IS an unlock; unlock demands the ceremony, so the bool API refuses."""
    op, tok = _unlock_with_dance(bare)                     # door now unlocked
    i = Intent("house_a", "lock", "front_door", "lock")
    r = bare.router.execute(i, op)                         # L2, not confirm-required: executes
    assert r.status == "executed" and r.rollback_token
    assert bare.router.rollback(r.rollback_token, op) is False
    assert bare.state.get_state("house_a.lock.front_door") == "locked"
    assert any(rec.status == "refused" and "first-class intent" in rec.message
               for rec in bare.audit.records)


def test_delegation_grant_requires_an_owner():
    reg = DelegationRegistry()
    d = Delegation(id="d", grantor="mallory", house_id="house_a", subsystem="alarm", action="arm")
    for kind in ("ai", "guest", "system"):
        with pytest.raises(PermissionError):
            reg.grant(d, Operator(kind, "house_a", kind))
    assert len(reg) == 0


def test_delegation_grant_respects_scope_and_role_cap():
    reg = DelegationRegistry()
    d = Delegation(id="d", grantor="colton", house_id="house_a", subsystem="battery", action="set_mode")
    with pytest.raises(PermissionError):
        reg.grant(d, Operator("owner", "house_b", "b-owner", houses={"house_b"}))
    with pytest.raises(PermissionError):
        reg.grant(d, Operator("owner", "house_a", "limited", max_level=2))
    assert reg.grant(d, Operator("owner", "house_a", "colton")) is d
