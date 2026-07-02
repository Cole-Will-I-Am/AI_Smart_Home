"""Part 3 — identity, roles & capabilities (RBAC), per-property scope."""
from homeops.identity import IdentityStore, operator_for
from homeops.permissions import Intent


def test_authenticate_returns_scoped_principal():
    ids = IdentityStore()
    tok = ids.register("alice", "owner")
    p = ids.authenticate(tok)
    assert p and p.id == "alice" and p.role.name == "owner"
    assert ids.authenticate("wrong-token") is None       # only the hash is stored; bad token fails


def test_estate_manager_capped_at_L2(world):
    tok = world.identity.register("mgr", "estate_manager", houses=["house_a"])
    op = operator_for(world.identity.authenticate(tok), "house_a")
    # L2 (arm) is within capability
    assert world.router.execute(Intent("house_a", "alarm", "panel", "arm", {"mode": "night"}), op).status == "executed"
    # L3 (generator) exceeds the role cap
    r = world.router.execute(Intent("house_a", "generator", "main", "start"), op)
    assert r.status == "refused" and "role" in r.message.lower()


def test_property_scope_enforced(world):
    tok = world.identity.register("mgr2", "estate_manager", houses=["house_a"])
    op = operator_for(world.identity.authenticate(tok), "house_b")   # active house not in scope
    r = world.router.execute(Intent("house_b", "light", "kitchen", "turn_on"), op)
    assert r.status == "refused" and "scope" in r.message.lower()


def test_monitor_is_observe_plus_routine_only(world):
    tok = world.identity.register("noc", "monitor")
    op = operator_for(world.identity.authenticate(tok), "house_a")
    assert world.router.execute(Intent("house_a", "light", "living_room", "turn_on"), op).status == "executed"   # L1
    assert world.router.execute(Intent("house_a", "lock", "front_door", "lock"), op).status == "refused"          # L2 blocked


def test_installer_has_setup_authority(world):
    tok = world.identity.register("installer1", "installer", houses=["house_a"])
    op = operator_for(world.identity.authenticate(tok), "house_a")
    r1 = world.router.execute(Intent("house_a", "generator", "main", "start"), op)   # L3 allowed for installer
    assert r1.status == "confirm_required"   # still needs confirmation, but not role-blocked
    r2 = world.router.execute(Intent("house_a", "generator", "main", "start", confirm_token=r1.confirm_token), op)
    assert r2.status == "executed"
