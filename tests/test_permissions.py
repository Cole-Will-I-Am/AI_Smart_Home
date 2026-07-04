import pytest

from homeops.permissions import Intent, Operator


def owner():
    return Operator("owner", "house_a")


def ai():
    return Operator("ai", "house_a")


def system():
    return Operator("system", "house_a", "local-automations")


PREVIOUSLY_UNGATED_L3_ACTIONS = [
    ("battery", "main", "set_mode", {"mode": "backup"}),
    ("climate", "thermostat_main", "set_mode", {"value": "eco"}),
    ("evcharger", "main", "set_limit", {"amps": 16}),
    ("hvac", "main", "emergency_shutoff", {}),
    ("power", "breaker_ev", "breaker_on", {}),
    ("power", "load_shed", "load_shed", {"tier": "nonessential"}),
    ("water", "main_valve", "open_main", {}),
]


def _l3_intent(case, *, confirm_token=None, emergency=False):
    subsystem, target, action, args = case
    return Intent("house_a", subsystem, target, action, dict(args),
                  confirm_token=confirm_token, emergency=emergency)


def _approve_l3_target(bare, case):
    subsystem, target, _, _ = case
    ent = bare.state.entity(f"house_a.{subsystem}.{target}")
    assert ent is not None
    ent.approved_hardware = True


def test_L1_routine_executes_directly(bare):
    r = bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner())
    assert r.status == "executed"
    assert bare.state.get_state("house_a.light.living_room") == "on"


def test_ai_L4_is_recommend_only(bare):
    r = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock_unknown"), ai())
    assert r.status == "recommend_only" and r.level == 4


def test_ai_may_do_L1(bare):
    r = bare.router.execute(Intent("house_a", "light", "kitchen", "turn_on"), ai())
    assert r.status == "executed"


def test_ai_L2_needs_human_confirm_and_gets_no_token(bare):
    r = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), ai())
    assert r.status == "confirm_required"
    assert r.confirm_token is None   # the AI cannot self-confirm


def test_owner_L2_confirm_then_execute(bare):
    r1 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), owner())
    assert r1.status == "confirm_required" and r1.confirm_token
    r2 = bare.router.execute(
        Intent("house_a", "lock", "front_door", "unlock", confirm_token=r1.confirm_token), owner())
    assert r2.status == "executed"
    assert bare.state.get_state("house_a.lock.front_door") == "unlocked"


def test_confirm_token_is_single_use(bare):
    r1 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), owner())
    tok = r1.confirm_token
    bare.router.execute(Intent("house_a", "lock", "front_door", "unlock", confirm_token=tok), owner())
    r3 = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock", confirm_token=tok), owner())
    assert r3.status == "confirm_required"   # token already consumed


def test_cross_house_guard(bare):
    r = bare.router.execute(Intent("house_b", "light", "kitchen", "turn_on"), owner())
    assert r.status == "confirm_required" and "cross-house" in r.message
    r2 = bare.router.execute(
        Intent("house_b", "light", "kitchen", "turn_on", confirm_cross_house=True), owner())
    assert r2.status == "executed"


def test_L5_prohibited(bare):
    r = bare.router.execute(Intent("house_a", "safety", "panel", "bypass"), owner())
    assert r.status == "prohibited"


def test_L3_requires_approved_hardware(bare):
    # climate.set_mode is L3, and the thermostat is not approved hardware. The confirmation gate
    # comes first; with a valid token, the downstream hardware gate still refuses it.
    r1 = bare.router.execute(Intent("house_a", "climate", "thermostat_main", "set_mode", {"value": "eco"}), owner())
    assert r1.status == "confirm_required" and r1.confirm_token
    r2 = bare.router.execute(
        Intent("house_a", "climate", "thermostat_main", "set_mode", {"value": "eco"},
               confirm_token=r1.confirm_token), owner())
    assert r2.status == "refused" and "approved" in r2.message


def test_L3_approved_hardware_with_confirm(bare):
    r1 = bare.router.execute(Intent("house_a", "generator", "main", "start"), owner())
    assert r1.status == "confirm_required" and r1.confirm_token
    r2 = bare.router.execute(
        Intent("house_a", "generator", "main", "start", confirm_token=r1.confirm_token), owner())
    assert r2.status == "executed"


@pytest.mark.parametrize("case", PREVIOUSLY_UNGATED_L3_ACTIONS)
def test_previously_ungated_L3_requires_confirmation_without_token(bare, case):
    _approve_l3_target(bare, case)
    r = bare.router.execute(_l3_intent(case), owner())
    assert r.status == "confirm_required"
    assert r.level == 3
    assert r.confirm_token


@pytest.mark.parametrize("case", PREVIOUSLY_UNGATED_L3_ACTIONS)
def test_previously_ungated_L3_valid_token_executes(bare, case):
    _approve_l3_target(bare, case)
    r1 = bare.router.execute(_l3_intent(case), owner())
    assert r1.status == "confirm_required" and r1.confirm_token
    r2 = bare.router.execute(_l3_intent(case, confirm_token=r1.confirm_token), owner())
    assert r2.status == "executed"


@pytest.mark.parametrize("case", PREVIOUSLY_UNGATED_L3_ACTIONS)
def test_previously_ungated_L3_system_operator_executes_without_token(bare, case):
    _approve_l3_target(bare, case)
    r = bare.router.execute(_l3_intent(case), system())
    assert r.status == "executed"


@pytest.mark.parametrize("case", PREVIOUSLY_UNGATED_L3_ACTIONS)
def test_previously_ungated_L3_emergency_intent_executes_without_token(bare, case):
    _approve_l3_target(bare, case)
    r = bare.router.execute(_l3_intent(case, emergency=True), owner())
    assert r.status == "executed"


def test_evcharger_zero_amps_is_off_but_three_amps_still_fails(bare):
    off = bare.router.execute(Intent("house_a", "evcharger", "main", "set_limit", {"amps": 0}), owner())
    assert off.status == "confirm_required" and off.confirm_token
    confirmed = bare.router.execute(
        Intent("house_a", "evcharger", "main", "set_limit", {"amps": 0},
               confirm_token=off.confirm_token), owner())
    assert confirmed.status == "executed"
    too_low = bare.router.execute(Intent("house_a", "evcharger", "main", "set_limit", {"amps": 3}), owner())
    assert too_low.status == "confirm_required" and "envelope" in too_low.message


def test_all_refusals_are_logged(bare):
    bare.router.execute(Intent("house_a", "lock", "front_door", "unlock_unknown"), ai())
    bare.router.execute(Intent("house_a", "safety", "panel", "bypass"), owner())
    statuses = {r.status for r in bare.audit.records}
    assert "recommend_only" in statuses and "prohibited" in statuses
