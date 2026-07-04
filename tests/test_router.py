from homeops.permissions import Intent, Operator
from homeops.simulator import devices


def owner():
    return Operator("owner", "house_a")


def test_rate_limit(bare):
    bare.engine._rate_limit = 5   # pin the guard: test the mechanism, not the (now-generous) default
    statuses = [bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner()).status
                for _ in range(8)]
    assert statuses.count("executed") <= 5
    assert any(r.status == "refused" and "rate" in r.message for r in bare.audit.records)


def test_rollback(bare):
    r = bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner())
    assert bare.state.get_state("house_a.light.living_room") == "on" and r.rollback_token
    assert bare.router.rollback(r.rollback_token, owner()) is True
    assert bare.state.get_state("house_a.light.living_room") == "off"


def test_rollback_undo_exception_audits_error_not_success(bare):
    r = bare.router.execute(Intent("house_a", "light", "living_room", "turn_on"), owner())
    assert r.rollback_token
    original_undo = bare.adapter.undo

    def boom(undo):
        raise RuntimeError("undo transport failed")

    bare.adapter.undo = boom
    try:
        assert bare.router.rollback(r.rollback_token, owner()) is False
    finally:
        bare.adapter.undo = original_undo
    rec = bare.audit.records[-1]
    assert rec.status == "error" and "state UNKNOWN" in rec.message
    assert rec.message != "rollback applied"


def test_valve_takes_ticks_to_close(bare):
    r1 = bare.router.execute(Intent("house_a", "water", "main_valve", "shutoff_main"), owner())
    r2 = bare.router.execute(
        Intent("house_a", "water", "main_valve", "shutoff_main", confirm_token=r1.confirm_token), owner())
    assert r2.status == "executed"
    assert bare.state.get_state("house_a.water.main_valve") == "closing"
    bare.tick(2)
    assert bare.state.get_state("house_a.water.main_valve") == "closed"


def test_out_of_envelope_thermostat_escalates_then_clamps(bare):
    # Part 14: the ENGINE gates the value first (the live HA adapter forwards args raw, so the
    # old behaviour — relying on the simulator's clamp — tested the wrong layer).
    raw = bare.router.execute(
        Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 200}), owner())
    assert raw.status == "confirm_required" and "envelope" in raw.message
    confirmed = bare.router.execute(
        Intent("house_a", "climate", "thermostat_main", "set_temperature", {"temperature": 200},
               confirm_token=raw.confirm_token), owner())
    assert confirmed.status == "executed"   # explicit, audited human exception
    assert bare.state.get_state("house_a.climate.thermostat_main") == 82   # device clamp still applies


def test_failed_destructive_action_does_not_start_cooldown_but_success_does(bare):
    op = owner()
    devices.inject_generator_fail(bare.state, "house_a.generator.main")
    first = bare.router.execute(Intent("house_a", "generator", "main", "start"), op)
    failed = bare.router.execute(
        Intent("house_a", "generator", "main", "start", confirm_token=first.confirm_token), op)
    assert failed.status == "unverified"

    devices.clear_faults(bare.state, "house_a.generator.main")
    bare.health.heartbeat("house_a.generator.main", bare.engine.tick)
    retry = bare.router.execute(Intent("house_a", "generator", "main", "start"), op)
    succeeded = bare.router.execute(
        Intent("house_a", "generator", "main", "start", confirm_token=retry.confirm_token), op)
    assert succeeded.status == "executed"

    again = bare.router.execute(Intent("house_a", "generator", "main", "start"), op)
    blocked = bare.router.execute(
        Intent("house_a", "generator", "main", "start", confirm_token=again.confirm_token), op)
    assert blocked.status == "refused" and "cooldown" in blocked.message


def test_safety_critical_rollback_succeeds_after_readback(bare):
    op = owner()
    first = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), op)
    unlocked = bare.router.execute(
        Intent("house_a", "lock", "front_door", "unlock", confirm_token=first.confirm_token), op)
    assert unlocked.status == "executed" and unlocked.rollback_token

    assert bare.router.rollback(unlocked.rollback_token, op) is True
    assert bare.state.get_state("house_a.lock.front_door") == "locked"
    assert bare.audit.records[-1].status == "rollback"


def test_safety_critical_rollback_readback_mismatch_is_unverified(bare):
    op = owner()
    first = bare.router.execute(Intent("house_a", "lock", "front_door", "unlock"), op)
    unlocked = bare.router.execute(
        Intent("house_a", "lock", "front_door", "unlock", confirm_token=first.confirm_token), op)
    assert unlocked.status == "executed" and unlocked.rollback_token
    original_undo = bare.adapter.undo

    def no_op(undo):
        pass

    bare.adapter.undo = no_op
    try:
        assert bare.router.rollback(unlocked.rollback_token, op) is False
    finally:
        bare.adapter.undo = original_undo
    rec = bare.audit.records[-1]
    assert rec.status == "unverified"
    assert bare.state.get_state("house_a.lock.front_door") == "unlocked"
