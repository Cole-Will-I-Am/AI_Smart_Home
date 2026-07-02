from homeops.permissions import Intent, Operator


def owner():
    return Operator("owner", "house_a")


def ai():
    return Operator("ai", "house_a")


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
    # climate.set_mode is L3, but a thermostat is not approved hardware -> refused
    r = bare.router.execute(Intent("house_a", "climate", "thermostat_main", "set_mode", {"value": "eco"}), owner())
    assert r.status == "refused" and "approved" in r.message


def test_L3_approved_hardware_with_confirm(bare):
    r1 = bare.router.execute(Intent("house_a", "generator", "main", "start"), owner())
    assert r1.status == "confirm_required" and r1.confirm_token
    r2 = bare.router.execute(
        Intent("house_a", "generator", "main", "start", confirm_token=r1.confirm_token), owner())
    assert r2.status == "executed"


def test_all_refusals_are_logged(bare):
    bare.router.execute(Intent("house_a", "lock", "front_door", "unlock_unknown"), ai())
    bare.router.execute(Intent("house_a", "safety", "panel", "bypass"), owner())
    statuses = {r.status for r in bare.audit.records}
    assert "recommend_only" in statuses and "prohibited" in statuses
