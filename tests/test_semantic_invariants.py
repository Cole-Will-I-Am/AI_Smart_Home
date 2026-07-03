"""Part 14 — semantic invariants: authority quantifies over arguments, not just verbs.

Review finding R-1: the 60–82°F clamp lived only in the simulator's device model; the live
HA adapter forwards `temperature` raw. These tests pin the fix at the correct layer — the
permission engine — so the guarantee is adapter-independent."""
from datetime import datetime

import pytest

from homeops import build_world
from homeops.permissions import Intent, Operator


def at(hour):
    return lambda: datetime(2026, 1, 15, hour, 30)


@pytest.fixture
def w():
    world = build_world(register_automations=False)
    world.router.clock = at(12)   # deterministic daytime clock
    return world


AI = Operator(kind="ai", active_house="house_a", name="claude")
OWNER = Operator(kind="owner", active_house="house_a", name="colton")
GUEST = Operator(kind="guest", active_house="house_a", name="visitor")
SYSTEM = Operator(kind="system", active_house="house_a", name="automations")


def temp(t, tok=None):
    return Intent("house_a", "climate", "thermostat_main", "set_temperature",
                  {"temperature": t}, confirm_token=tok)


def test_ai_out_of_envelope_escalates_with_no_token(w):
    r = w.router.execute(temp(45), AI)
    assert r.status == "confirm_required"
    assert r.confirm_token is None          # the AI never receives a usable token
    assert "envelope" in r.message


def test_ai_in_envelope_executes_directly(w):
    assert w.router.execute(temp(68), AI).status == "executed"


def test_owner_out_of_envelope_gets_token_and_can_explicitly_override(w):
    r1 = w.router.execute(temp(48), OWNER)
    assert r1.status == "confirm_required" and r1.confirm_token
    r2 = w.router.execute(temp(48, tok=r1.confirm_token), OWNER)
    assert r2.status == "executed"          # explicit, single-use, audited human exception


def test_override_token_binds_to_the_exact_args(w):
    r1 = w.router.execute(temp(48), OWNER)
    r2 = w.router.execute(temp(47, tok=r1.confirm_token), OWNER)   # different value, same token
    assert r2.status == "confirm_required"


def test_guest_out_of_envelope_receives_no_token(w):
    r = w.router.execute(temp(200), GUEST)
    assert r.status == "confirm_required" and r.confirm_token is None


def test_system_operators_are_exempt(w):
    # Local automations run below the AI with reviewed values; a comfort envelope must never
    # be able to block an emergency response (e.g. freeze-protect).
    assert w.router.execute(temp(45), SYSTEM).status == "executed"


def test_non_numeric_argument_fails_closed(w):
    r = w.router.execute(temp("scorching"), OWNER)
    assert r.status == "confirm_required" and "not numeric" in r.message


def test_quiet_hours_gate_announcements(w):
    def ann():
        return Intent("house_a", "speaker", "intercom", "announce", {"message": "hi"})
    w.router.clock = at(3)
    assert w.router.execute(ann(), AI).status == "confirm_required"
    w.router.clock = at(15)
    assert w.router.execute(ann(), AI).status == "executed"


def test_ev_charger_amp_envelope(w):
    r = w.router.execute(Intent("house_a", "evcharger", "main", "set_limit", {"amps": 80}), OWNER)
    assert r.status == "confirm_required" and "envelope" in r.message
